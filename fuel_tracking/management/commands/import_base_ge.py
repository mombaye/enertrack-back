# fuel_tracking/management/commands/import_base_ge.py
"""
Importe "Base GE.xlsx" (feuille Feuil32) — source PRINCIPALE du tableau
Suivis Consommation (demande explicite 2026-08, "Pour faire simple toutes
les informations du tableau ... on le récupère depuis le dernier fichier").

Colonnes importées (B à AF) :
  Site ID (B), NOM (C), Typologie réelle (H), Typo simple (I),
  On-Grid/Off-Grid (M), Type de GE (N), Puissance GE KVA (O),
  GE fuel conso [L/Month] (P), Rectifier efficiency (S),
  GE load percentage (T), SPC L/kWh (U), Cph L/h (V),
  Durée de fonctionnement du GE en h (X), Consommation de carburant L (Y,
  = Cph L/h × Durée), Fuel Consumption (L) (AF, bilan de cuve — stock
  initial + ajouts − stock final).

Rectifier efficiency et SPC (colonnes S/U) ne sont PLUS affichées comme
colonnes du tableau (constantes uniques 0.8/0.27 sur les 469 lignes,
vérifié 2026-08) mais restent importées dans FuelCphGeParameter, qui
continue d'alimenter le pipeline CPH Snowflake (seule source pour
Énergie site / Batterie DC / Batterie AC / Énergie GE, absentes de ce
fichier).

"Mêmes sites exacts que le fichier" (demande explicite) : les 469 sites du
fichier sont importés tels quels, y compris ceux sans ligne
FuelConsommationMonthly existante pour le mois choisi (créée, pas
seulement mise à jour) — contrairement à sync_fuel_cph qui ne fait qu'UPDATE.

Usage:
    docker compose exec web python manage.py import_base_ge --file=data_imports/base_ge.xlsx --month=2026-08 --dry-run
    docker compose exec web python manage.py import_base_ge --file=data_imports/base_ge.xlsx --month=2026-08
"""
from datetime import date
from decimal import Decimal, InvalidOperation

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelCphGeParameter, FuelConsommationMonthly

SHEET_NAME = "Feuil32"
HEADER_ROW = 4

# Indices 0-based dans chaque tuple de ligne (colonnes B à AF)
COL_SITE_ID = 1
COL_NOM = 2
COL_TYPOLOGIE_REELLE = 7
COL_TYPO_SIMPLE = 8
COL_TYPE_SITE = 12  # On-Grid / Off-Grid
COL_TYPE_GE = 13
COL_PGE_KVA = 14
COL_CONSO_L_MONTH = 15
COL_RECT_EFF = 18
COL_GE_LOAD_PCT = 19
COL_SPC = 20
COL_CPH_LPH = 21
COL_RUNTIME_H = 23
COL_CONSO_ESTIMEE = 24  # Consommation de carburant en L (= Cph L/h × Durée)
COL_CONSO_MESUREE = 31  # Fuel Consumption (L) — bilan de cuve

PARAMETER_SOURCE_LABEL = "Base GE.xlsx — SPC/rendement uniformes (hypothèse validée), PGE_KVA réel"
FICHIER_SOURCE_LABEL = "Base GE.xlsx"


class Command(BaseCommand):
    help = "Importe Base GE.xlsx (Feuil32) — source principale du tableau Suivis Consommation."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--month", required=True, help="YYYY-MM — mois auquel attribuer les valeurs du fichier dans FuelConsommationMonthly.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        month_str = options["month"]
        dry_run = options["dry_run"]
        year, month = (int(x) for x in month_str.split("-"))
        month_year = f"{year:04d}-{month:02d}"

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT BASE GE (source principale Suivis Consommation)")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Fichier      : {path}")
        self.stdout.write(f"  Mois cible   : {month_year}")
        self.stdout.write(f"  Dry run      : {dry_run}")

        try:
            wb = openpyxl.load_workbook(path, data_only=True)
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"\n  Fichier introuvable : {path}\n"))
            return

        ws = wb[SHEET_NAME]

        def to_decimal(v):
            return Decimal(str(v)) if v is not None else None

        def to_str(v):
            s = str(v).strip() if v is not None else None
            return s or None

        rows = []
        errors = []
        for i, row in enumerate(ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True)):
            excel_row = HEADER_ROW + 1 + i
            site_id = row[COL_SITE_ID]
            if not site_id:
                continue
            site_id = str(site_id).strip()
            try:
                pge_kva = to_decimal(row[COL_PGE_KVA])
                conso_l_month = to_decimal(row[COL_CONSO_L_MONTH])
                rect_eff = to_decimal(row[COL_RECT_EFF])
                ge_load_pct = to_decimal(row[COL_GE_LOAD_PCT])
                spc = to_decimal(row[COL_SPC])
                cph_lph = to_decimal(row[COL_CPH_LPH])
                runtime_h = to_decimal(row[COL_RUNTIME_H])
                conso_estimee = to_decimal(row[COL_CONSO_ESTIMEE])
                conso_mesuree = to_decimal(row[COL_CONSO_MESUREE])
            except (InvalidOperation, ValueError, TypeError) as e:
                errors.append({"row": excel_row, "site_id": site_id, "error": f"Valeur invalide : {e}"})
                continue

            if pge_kva is not None and pge_kva <= 0:
                errors.append({"row": excel_row, "site_id": site_id, "error": "PGE_KVA doit être > 0 — ignoré"})
                pge_kva = None
            if rect_eff is not None and not (0 < rect_eff <= 1):
                errors.append({"row": excel_row, "site_id": site_id, "error": "Rectifier efficiency hors ]0,1] — ignoré"})
                rect_eff = None
            if spc is not None and spc <= 0:
                errors.append({"row": excel_row, "site_id": site_id, "error": "SPC doit être > 0 — ignoré"})
                spc = None
            if runtime_h is not None and not (0 <= runtime_h <= 744):  # 744h = 31 jours × 24h, borne large
                errors.append({"row": excel_row, "site_id": site_id, "error": "Running time hors bornes plausibles (0-744h) — ignoré"})
                runtime_h = None

            rows.append({
                "site_id": site_id,
                "site_name": to_str(row[COL_NOM]),
                "typologie_reelle": to_str(row[COL_TYPOLOGIE_REELLE]),
                "typo_simple": to_str(row[COL_TYPO_SIMPLE]),
                "type_site": to_str(row[COL_TYPE_SITE]),
                "type_ge": to_str(row[COL_TYPE_GE]),
                "pge_kva": pge_kva,
                "conso_l_month": conso_l_month,
                "rectifier_efficiency_ratio": rect_eff,
                "ge_load_pct": ge_load_pct,
                "spc_l_per_kwh": spc,
                "cph_lph": cph_lph,
                "runtime_h": runtime_h,
                "conso_estimee_l": conso_estimee,
                "conso_mesuree_l": conso_mesuree,
            })

        self.stdout.write(f"\n  Sites lus : {len(rows)}")
        if errors:
            self.stdout.write(self.style.WARNING(f"  Valeurs rejetées : {len(errors)}"))
            for e in errors[:20]:
                self.stdout.write(f"    ligne {e['row']} ({e['site_id']}) : {e['error']}")

        if dry_run:
            for r in rows[:10]:
                self.stdout.write(
                    f"    {r['site_id']} | {r['typologie_reelle']}/{r['typo_simple']} | {r['type_site']} | {r['type_ge']} | "
                    f"PGE={r['pge_kva']} kVA | runtime={r['runtime_h']} h | Cph={r['cph_lph']} L/h | "
                    f"estimée={r['conso_estimee_l']} L | mesurée={r['conso_mesuree_l']} L"
                )
            self.stdout.write(self.style.WARNING(f"\n  DRY RUN — aucune donnée écrite ({len(rows)} site(s) prêts).\n"))
            return

        cph_created = cph_updated = 0
        fcm_created = fcm_updated = 0
        valid_from = date(year, 1, 1)

        with transaction.atomic():
            for r in rows:
                if r["spc_l_per_kwh"] is not None and r["rectifier_efficiency_ratio"] is not None:
                    _, is_created = FuelCphGeParameter.objects.update_or_create(
                        site_id=r["site_id"], valid_from=valid_from,
                        defaults={
                            "valid_to": None,
                            "pge_kva": r["pge_kva"],
                            "rectifier_efficiency_ratio": r["rectifier_efficiency_ratio"],
                            "spc_l_per_kwh": r["spc_l_per_kwh"],
                            "parameter_source": PARAMETER_SOURCE_LABEL,
                        },
                    )
                    if is_created:
                        cph_created += 1
                    else:
                        cph_updated += 1

                fc, is_created = FuelConsommationMonthly.objects.get_or_create(
                    month_year=month_year, site_id=r["site_id"],
                    defaults={"year": year, "month": month, "site_name": r["site_name"]},
                )
                update_fields = []

                def set_field(field, value):
                    if value is not None:
                        setattr(fc, field, value)
                        update_fields.append(field)

                if r["site_name"] and not fc.site_name:
                    fc.site_name = r["site_name"]
                    update_fields.append("site_name")
                set_field("typology_fichier", r["typologie_reelle"])
                set_field("typo_simple_fichier", r["typo_simple"])
                set_field("site_type_fichier", r["type_site"])
                set_field("type_ge_fichier", r["type_ge"])
                set_field("pge_kva_fichier", r["pge_kva"])
                set_field("conso_fichier_l", r["conso_l_month"])
                set_field("ge_load_pct_fichier", r["ge_load_pct"])
                set_field("cph_lph_fichier", r["cph_lph"])
                set_field("ge_runtime_fichier_h", r["runtime_h"])
                set_field("conso_estimee_fichier_l", r["conso_estimee_l"])
                set_field("conso_mesuree_fichier_l", r["conso_mesuree_l"])
                if update_fields:
                    fc.fichier_source = FICHIER_SOURCE_LABEL
                    update_fields.append("fichier_source")
                    fc.save(update_fields=update_fields)
                if is_created:
                    fcm_created += 1
                else:
                    fcm_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  FuelCphGeParameter        : {cph_created} créée(s), {cph_updated} mise(s) à jour.\n"
            f"  FuelConsommationMonthly ({month_year}) : {fcm_created} créée(s), {fcm_updated} mise(s) à jour.\n"
        ))
