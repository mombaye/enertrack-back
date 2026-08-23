# fuel_tracking/management/commands/import_gardien_conso.py
"""
Importe "Esco Senegal - Synthèse Conso Fuel [Mois] [Année].xlsb", onglet
"Synthese conso fuel" — relevés manuels des sociétés de gardiennage, pour
les sites sans capteur Snowflake ni télémétrie GE exploitable (2026-08 :
298 sites "Avec GE mais aucune donnée", 290 présents dans ce fichier).

Colonne retenue : "Qtité cons considéréé (L)" — la valeur finale validée
par l'équipe après analyse (relevé de jauge physique par le gardien), PAS
"Conso Moyenne théorique [L]" (quasi vide, 14/298 sites, et divergente de
plusieurs ordres de grandeur quand elle existe — décision utilisateur
2026-08 après vérification).

Le fichier daté "Juillet 2026" ne couvre QUE juillet (colonnes "Relevés
JUILLET 2026" / "Estimation Consommation Mois de JUILLET 2026") — importé
tel quel dans FuelConsommationMonthly pour month_year=2026-07 (décision
utilisateur explicite : ne PAS reporter ces valeurs sur août, qui n'est
pas couvert par ce fichier).

Une ligne est ignorée (pas importée) si "Date de Relevé Finale" est vide
ET la valeur considérée vaut 0 : ce n'est pas une vraie mesure de zéro,
juste une période pas encore clôturée à l'export du fichier — "sans
donnée inventée".

L'onglet "Synthese conso fuel N-1" a une structure de colonnes DIFFÉRENTE
(vérifié 2026-08, colonnes décalées) et n'est pas traité ici.

Usage:
    docker compose exec web python manage.py import_gardien_conso --file=data_imports/synthese_conso_fuel_juillet_2026.xlsb --month=2026-07 --dry-run
    docker compose exec web python manage.py import_gardien_conso --file=data_imports/synthese_conso_fuel_juillet_2026.xlsb --month=2026-07
"""
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelConsommationMonthly

SHEET_NAME = "Synthese conso fuel"
HEADER_SKIPROWS = 14  # ligne d'en-tête réelle (3e sous-ligne d'un en-tête fusionné sur 3 lignes)

COL_SITE_ID = 2
COL_SITE_NAME = 3
COL_DATE_RELEVE_FINALE = 81
COL_QTITE_CONSIDEREE = 140
COL_STATUT = 193

SITE_ID_RE = re.compile(r"^[A-Z]{3}_\d{4}$")


class Command(BaseCommand):
    help = 'Importe "Synthese conso fuel" (relevés gardiennage) — Conso mesurée pour les sites sans capteur Snowflake.'

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--month", required=True, help="YYYY-MM — mois réellement couvert par le fichier (ex: 2026-07 pour un fichier 'Juillet 2026').")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        month_str = options["month"]
        dry_run = options["dry_run"]
        year, month = (int(x) for x in month_str.split("-"))
        month_year = f"{year:04d}-{month:02d}"

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT GARDIENNAGE — Conso mesurée (relevés manuels)")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Fichier      : {path}")
        self.stdout.write(f"  Onglet       : {SHEET_NAME}")
        self.stdout.write(f"  Mois cible   : {month_year}")
        self.stdout.write(f"  Dry run      : {dry_run}")

        try:
            df = pd.read_excel(path, sheet_name=SHEET_NAME, engine="pyxlsb", header=None, skiprows=HEADER_SKIPROWS)
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"\n  Fichier introuvable : {path}\n"))
            return

        def to_decimal(v):
            try:
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return None
                return Decimal(str(v))
            except (InvalidOperation, ValueError, TypeError):
                return None

        rows = []
        skipped_not_closed = 0
        skipped_bad_id = 0
        skipped_negative = 0

        for _, row in df.iterrows():
            raw_id = row.get(COL_SITE_ID)
            if raw_id is None or (isinstance(raw_id, float) and pd.isna(raw_id)):
                continue
            site_id = str(raw_id).strip()
            if not SITE_ID_RE.match(site_id):
                skipped_bad_id += 1
                continue

            considered = to_decimal(row.get(COL_QTITE_CONSIDEREE))
            date_finale = row.get(COL_DATE_RELEVE_FINALE)
            date_finale_str = None if (date_finale is None or (isinstance(date_finale, float) and pd.isna(date_finale))) else str(date_finale).strip()

            if considered is None:
                continue
            if considered < 0:
                # Anomalie de données (ex: livraison de carburant non
                # comptabilisée entre 2 relevés) — une "consommation"
                # négative est physiquement impossible, jamais importée
                # telle quelle (vérifié 2026-08 : 75 lignes concernées sur
                # ce fichier, ex. -3 727 L).
                skipped_negative += 1
                continue
            if date_finale_str is None and considered == 0:
                skipped_not_closed += 1
                continue

            statut = row.get(COL_STATUT)
            statut_str = str(statut).strip() if statut is not None and not (isinstance(statut, float) and pd.isna(statut)) else None
            site_name = row.get(COL_SITE_NAME)
            site_name_str = str(site_name).strip() if site_name is not None and not (isinstance(site_name, float) and pd.isna(site_name)) else None

            rows.append({
                "site_id": site_id,
                "site_name": site_name_str,
                "conso_gardien_l": considered,
                "gardien_statut": statut_str,
                "gardien_date_releve_finale": date_finale_str,
            })

        self.stdout.write(f"\n  Sites lus (valides) : {len(rows)}")
        self.stdout.write(f"  Ignorés (ID invalide)               : {skipped_bad_id}")
        self.stdout.write(f"  Ignorés (valeur négative, anomalie) : {skipped_negative}")
        self.stdout.write(f"  Ignorés (période non clôturée, 0 L) : {skipped_not_closed}")

        if dry_run:
            for r in rows[:10]:
                self.stdout.write(
                    f"    {r['site_id']} | {r['site_name']} | considérée={r['conso_gardien_l']} L | "
                    f"statut={r['gardien_statut']} | relevé final={r['gardien_date_releve_finale']}"
                )
            self.stdout.write(self.style.WARNING(f"\n  DRY RUN — aucune donnée écrite ({len(rows)} site(s) prêts).\n"))
            return

        source_label = f"{path.split('/')[-1]} — onglet {SHEET_NAME}"
        fcm_created = fcm_updated = 0

        with transaction.atomic():
            for r in rows:
                fc, is_created = FuelConsommationMonthly.objects.get_or_create(
                    month_year=month_year, site_id=r["site_id"],
                    defaults={"year": year, "month": month, "site_name": r["site_name"]},
                )
                fc.conso_gardien_l = r["conso_gardien_l"]
                fc.gardien_statut = r["gardien_statut"]
                fc.gardien_date_releve_finale = r["gardien_date_releve_finale"]
                fc.gardien_source = source_label
                update_fields = ["conso_gardien_l", "gardien_statut", "gardien_date_releve_finale", "gardien_source"]
                if r["site_name"] and not fc.site_name:
                    fc.site_name = r["site_name"]
                    update_fields.append("site_name")
                fc.save(update_fields=update_fields)
                if is_created:
                    fcm_created += 1
                else:
                    fcm_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  FuelConsommationMonthly ({month_year}) : {fcm_created} créée(s), {fcm_updated} mise(s) à jour.\n"
        ))
