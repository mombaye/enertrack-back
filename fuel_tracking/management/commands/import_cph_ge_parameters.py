# fuel_tracking/management/commands/import_cph_ge_parameters.py
"""
Importe le fichier de référence des paramètres GE (CPH_GE_PARAMETERS) dans
FuelCphGeParameter — colonne requise : SITE_ID, VALID_FROM, SPC_L_PER_KWH.
SPC_L_PER_KWH est le SEUL paramètre qui n'a aucune source Snowflake fiable
(voir FuelCphGeParameter docstring pour le détail de la vérification 2026-08)
— sans lui, aucun litre ne peut être calculé, quoi qu'il arrive.

Colonnes optionnelles : VALID_TO, PGE_KVA, POWER_FACTOR, RECTIFIER_EFFICIENCY_RATIO,
GE_TYPE, PARAMETER_SOURCE. PGE_KVA/GE_TYPE sont auto-sourcés depuis Snowflake
SITE_DG à chaque calcul (fuel_cph_snowflake.fetch_site_ge_specs) — ces
colonnes ne servent qu'à surcharger la valeur pour un site donné si besoin.
POWER_FACTOR retombe sur 0.8 (standard) si absent. RECTIFIER_EFFICIENCY_RATIO
retombe sur la moyenne mensuelle Snowflake (fetch_site_rectifier_efficiency,
~46% de couverture Sénégal vérifiée 2026-08) si absent — contrairement à
PGE_KVA/POWER_FACTOR, ce champ entre dans le calcul des litres : s'il manque
à la fois du fichier ET de Snowflake pour un site donné, ce site reste
MISSING_PARAMETER malgré un SPC valide.

Contrairement à import_cph_matrix.py (remplacement complet), ce modèle est
TEMPOREL : un ré-import ne doit jamais supprimer l'historique déjà clôturé
(valid_to renseigné) — nécessaire pour ré-exécuter/auditer des mois passés.
Upsert par (site_id, valid_from) ; toute ligne dont la plage [valid_from,
valid_to] chevauche une fiche existante (en base ou déjà acceptée plus haut
dans le même fichier) du même site est rejetée dans le rapport d'erreurs
plutôt qu'écrasée silencieusement.

Usage:
    docker compose exec web python manage.py import_cph_ge_parameters
    docker compose exec web python manage.py import_cph_ge_parameters --dry-run
    docker compose exec web python manage.py import_cph_ge_parameters --file=chemin.xlsx
"""
from datetime import date
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelCphGeParameter

DEFAULT_PATH = "data_imports/cph_ge_parameters.xlsx"

ALIASES = {
    "site_id": ["SITE_ID", "Site ID", "site_id"],
    "valid_from": ["VALID_FROM", "Valid From", "valid_from"],
    "valid_to": ["VALID_TO", "Valid To", "valid_to"],
    "pge_kva": ["PGE_KVA", "pge_kva"],
    "power_factor": ["POWER_FACTOR", "power_factor"],
    "rectifier_efficiency_ratio": ["RECTIFIER_EFFICIENCY_RATIO", "rectifier_efficiency_ratio"],
    "spc_l_per_kwh": ["SPC_L_PER_KWH", "spc_l_per_kwh"],
    "ge_type": ["GE_TYPE", "ge_type"],
    "parameter_source": ["PARAMETER_SOURCE", "parameter_source"],
}


def _pick(columns, key):
    for alias in ALIASES[key]:
        if alias in columns:
            return alias
    return None


class Command(BaseCommand):
    help = "Importe le fichier de référence des paramètres GE (CPH_GE_PARAMETERS) dans FuelCphGeParameter."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=DEFAULT_PATH)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT PARAMÈTRES GE (CPH)")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Lecture : {path}")

        try:
            df = pd.read_csv(path, dtype=object) if path.endswith(".csv") else pd.read_excel(path, dtype=object)
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"\n  Fichier introuvable : {path}\n"))
            return

        c_site = _pick(df.columns, "site_id")
        c_from = _pick(df.columns, "valid_from")
        c_to = _pick(df.columns, "valid_to")
        c_kva = _pick(df.columns, "pge_kva")
        c_pf = _pick(df.columns, "power_factor")
        c_eff = _pick(df.columns, "rectifier_efficiency_ratio")
        c_spc = _pick(df.columns, "spc_l_per_kwh")
        c_type = _pick(df.columns, "ge_type")
        c_source = _pick(df.columns, "parameter_source")

        # Seul SITE_ID/VALID_FROM/SPC_L_PER_KWH est requis : PGE_KVA/
        # POWER_FACTOR/GE_TYPE/RECTIFIER_EFFICIENCY_RATIO sont tous
        # auto-sourcés depuis Snowflake à chaque calcul si absents du
        # fichier (voir docstring module).
        required = [
            ("site_id", c_site), ("valid_from", c_from), ("spc_l_per_kwh", c_spc),
        ]
        missing = [k for k, v in required if not v]
        if missing:
            self.stdout.write(self.style.ERROR(f"\n  Colonnes manquantes : {missing}. Colonnes trouvées : {list(df.columns)}\n"))
            return

        # Fiches déjà en base, par site — pour la détection de chevauchement.
        # Complétée au fil de la boucle avec les lignes du fichier déjà
        # acceptées, pour attraper aussi les chevauchements INTRA-fichier.
        existing_by_site: dict[str, list[tuple]] = {}
        for p in FuelCphGeParameter.objects.all():
            existing_by_site.setdefault(p.site_id, []).append((p.valid_from, p.valid_to))

        rows = []
        errors = []

        for i, row in df.iterrows():
            excel_row = i + 2  # index base 0 -> 1-based, +1 pour la ligne d'en-tête
            try:
                site_id = str(row[c_site]).strip()
                valid_from = pd.to_datetime(row[c_from]).date()
                valid_to = pd.to_datetime(row[c_to]).date() if c_to and pd.notna(row.get(c_to)) else None
                # PGE_KVA/POWER_FACTOR/RECTIFIER_EFFICIENCY_RATIO optionnels —
                # auto-sourcés depuis Snowflake à chaque calcul si absents ici
                # (voir docstring module). None si colonne absente ou cellule vide.
                pge_kva = Decimal(str(row[c_kva])) if c_kva and pd.notna(row.get(c_kva)) else None
                power_factor = Decimal(str(row[c_pf])) if c_pf and pd.notna(row.get(c_pf)) else None
                rectifier_efficiency_ratio = Decimal(str(row[c_eff])) if c_eff and pd.notna(row.get(c_eff)) else None
                spc_l_per_kwh = Decimal(str(row[c_spc]))
                ge_type = str(row[c_type]).strip() if c_type and pd.notna(row.get(c_type)) else ""
                parameter_source = str(row[c_source]).strip() if c_source and pd.notna(row.get(c_source)) else ""
            except (InvalidOperation, ValueError, TypeError) as e:
                errors.append({"row": excel_row, "site_id": None, "error": f"Valeur invalide : {e}"})
                continue

            if not site_id or site_id.lower() == "nan":
                errors.append({"row": excel_row, "site_id": None, "error": "SITE_ID vide"})
                continue
            if pge_kva is not None and pge_kva <= 0:
                errors.append({"row": excel_row, "site_id": site_id, "error": "PGE_KVA doit être > 0 si renseigné"})
                continue
            if power_factor is not None and not (0 < power_factor <= 1):
                errors.append({"row": excel_row, "site_id": site_id, "error": "POWER_FACTOR doit être dans ]0, 1] si renseigné"})
                continue
            if rectifier_efficiency_ratio is not None and not (0 < rectifier_efficiency_ratio <= 1):
                errors.append({"row": excel_row, "site_id": site_id, "error": "RECTIFIER_EFFICIENCY_RATIO doit être dans ]0, 1] si renseigné"})
                continue
            if spc_l_per_kwh <= 0:
                errors.append({"row": excel_row, "site_id": site_id, "error": "SPC_L_PER_KWH doit être > 0"})
                continue
            if valid_to is not None and valid_to < valid_from:
                errors.append({"row": excel_row, "site_id": site_id, "error": "VALID_TO antérieur à VALID_FROM"})
                continue

            overlap_with = None
            for ex_from, ex_to in existing_by_site.get(site_id, []):
                if ex_from == valid_from:
                    continue  # même clé -> update_or_create met simplement à jour cette fiche
                ex_to_cmp = ex_to or date.max
                new_to_cmp = valid_to or date.max
                if valid_from <= ex_to_cmp and ex_from <= new_to_cmp:
                    overlap_with = (ex_from, ex_to)
                    break
            if overlap_with:
                errors.append({
                    "row": excel_row, "site_id": site_id,
                    "error": f"Chevauche une fiche existante ({overlap_with[0]} → {overlap_with[1] or '…'})",
                })
                continue

            existing_by_site.setdefault(site_id, []).append((valid_from, valid_to))
            rows.append({
                "site_id": site_id, "valid_from": valid_from, "valid_to": valid_to,
                "pge_kva": pge_kva, "power_factor": power_factor,
                "rectifier_efficiency_ratio": rectifier_efficiency_ratio,
                "spc_l_per_kwh": spc_l_per_kwh, "ge_type": ge_type, "parameter_source": parameter_source,
            })

        self.stdout.write(f"\n  Lignes valides   : {len(rows)}")
        self.stdout.write(f"  Lignes rejetées  : {len(errors)}")
        for err in errors[:20]:
            self.stdout.write(self.style.WARNING(f"    ligne {err['row']} ({err['site_id'] or '?'}) : {err['error']}"))
        if len(errors) > 20:
            self.stdout.write(self.style.WARNING(f"    ... et {len(errors) - 20} autre(s)"))

        if dry_run:
            for r in rows[:10]:
                kva_label = f"{r['pge_kva']} kVA" if r["pge_kva"] is not None else "kVA auto (Snowflake)"
                self.stdout.write(f"    {r['site_id']} | {r['valid_from']} → {r['valid_to'] or '…'} | {kva_label} | SPC {r['spc_l_per_kwh']} L/kWh")
            self.stdout.write(self.style.WARNING("\n  DRY RUN — aucune donnée écrite.\n"))
            return

        created = updated = 0
        with transaction.atomic():
            for r in rows:
                site_id = r.pop("site_id")
                valid_from = r.pop("valid_from")
                _, is_created = FuelCphGeParameter.objects.update_or_create(
                    site_id=site_id, valid_from=valid_from, defaults=r,
                )
                if is_created:
                    created += 1
                else:
                    updated += 1

        self.stdout.write(self.style.SUCCESS(f"\n  {created} créée(s), {updated} mise(s) à jour, {len(errors)} rejetée(s).\n"))
