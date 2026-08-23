# fuel_tracking/management/commands/import_kpis_par_site.py
"""
Importe "Base août 26 validée V1_cp_v3 1.xlsx" (feuille "KPIs par site") —
2e fichier de référence (2026-08), comble les trous laissés par
Base GE.xlsx sur Running Time / Conso estimée (colonnes X/Y de Feuil32,
renseignées pour seulement 5 des 469 lignes, motif manifestement factice —
voir import_base_ge.py). Ce fichier couvre ses 443 sites à 100% sur ces 2
métriques (⊂ les 469 sites de Base GE.xlsx, vérifié 2026-08).

Colonnes importées (A à O) : Site ID (A), Genset fuel conso — valeur
mensuelle déjà calculée dans le fichier (K), Genset running time [hrs/yr]
(L, ÷12 ici), Genset Production [kWh/y] (O, ÷12).

Ne touche JAMAIS aux champs sourcés de Base GE.xlsx (typologie/type de
site/type de GE/PGE_KVA/rectifier/SPC/GE load %/Cph L/h) — écrit
uniquement dans les colonnes _aout26 dédiées de FuelConsommationMonthly,
priorité résolue en LECTURE dans FuelConsommationListView.serialize().

Usage:
    docker compose exec web python manage.py import_kpis_par_site --file=data_imports/base_aout_26.xlsx --month=2026-08 --dry-run
    docker compose exec web python manage.py import_kpis_par_site --file=data_imports/base_aout_26.xlsx --month=2026-08
"""
from decimal import Decimal, InvalidOperation

import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelConsommationMonthly

SHEET_NAME = "KPIs par site"
HEADER_ROW = 6

# Indices 0-based dans chaque tuple de ligne (colonnes A à O)
COL_SITE_ID = 0
COL_SITE_NAME = 1
COL_CONSO_MONTH = 10  # Genset fuel conso, déjà mensuel (L/mois)
COL_RUNTIME_YR = 11  # Genset running time [hrs/yr]
COL_GE_PROD_YR = 14  # Genset Production [kWh/y]

FICHIER_SOURCE_LABEL = "Base août 26 validée V1_cp_v3 1.xlsx"


class Command(BaseCommand):
    help = 'Importe "Base août 26 validée" (KPIs par site) — comble Running Time / Conso estimée manquants de Base GE.xlsx.'

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--month", required=True, help="YYYY-MM")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        month_str = options["month"]
        dry_run = options["dry_run"]
        year, month = (int(x) for x in month_str.split("-"))
        month_year = f"{year:04d}-{month:02d}"

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT KPIs PAR SITE (Base août 26 validée — comble Running Time/Conso estimée)")
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

        rows = []
        errors = []
        for i, row in enumerate(ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True)):
            excel_row = HEADER_ROW + 1 + i
            site_id = row[COL_SITE_ID]
            if not site_id:
                continue
            site_id = str(site_id).strip()
            try:
                conso_month = to_decimal(row[COL_CONSO_MONTH])
                runtime_yr = to_decimal(row[COL_RUNTIME_YR])
                ge_prod_yr = to_decimal(row[COL_GE_PROD_YR])
            except (InvalidOperation, ValueError, TypeError) as e:
                errors.append({"row": excel_row, "site_id": site_id, "error": f"Valeur invalide : {e}"})
                continue

            runtime_h = (runtime_yr / 12) if runtime_yr is not None else None
            ge_prod_kwh = (ge_prod_yr / 12) if ge_prod_yr is not None else None

            if conso_month is not None and conso_month < 0:
                errors.append({"row": excel_row, "site_id": site_id, "error": "Conso mensuelle négative — ignorée"})
                conso_month = None
            if runtime_h is not None and not (0 <= runtime_h <= 744):
                errors.append({"row": excel_row, "site_id": site_id, "error": "Running time hors bornes plausibles (0-744h) — ignoré"})
                runtime_h = None

            rows.append({
                "site_id": site_id,
                "site_name": str(row[COL_SITE_NAME]).strip() if row[COL_SITE_NAME] else None,
                "conso_estimee_aout26_l": conso_month,
                "ge_runtime_aout26_h": runtime_h,
                "ge_prod_fichier_kwh": ge_prod_kwh,
            })

        self.stdout.write(f"\n  Sites lus : {len(rows)}")
        if errors:
            self.stdout.write(self.style.WARNING(f"  Valeurs rejetées : {len(errors)}"))
            for e in errors[:20]:
                self.stdout.write(f"    ligne {e['row']} ({e['site_id']}) : {e['error']}")

        if dry_run:
            for r in rows[:10]:
                self.stdout.write(
                    f"    {r['site_id']} | conso_estimee={r['conso_estimee_aout26_l']} L/mois | "
                    f"runtime={r['ge_runtime_aout26_h']} h | ge_prod={r['ge_prod_fichier_kwh']} kWh"
                )
            self.stdout.write(self.style.WARNING(f"\n  DRY RUN — aucune donnée écrite ({len(rows)} site(s) prêts).\n"))
            return

        fcm_created = fcm_updated = 0

        with transaction.atomic():
            for r in rows:
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
                set_field("conso_estimee_aout26_l", r["conso_estimee_aout26_l"])
                set_field("ge_runtime_aout26_h", r["ge_runtime_aout26_h"])
                set_field("ge_prod_fichier_kwh", r["ge_prod_fichier_kwh"])
                if update_fields:
                    fc.save(update_fields=update_fields)
                if is_created:
                    fcm_created += 1
                else:
                    fcm_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  FuelConsommationMonthly ({month_year}) : {fcm_created} créée(s), {fcm_updated} mise(s) à jour.\n"
        ))
