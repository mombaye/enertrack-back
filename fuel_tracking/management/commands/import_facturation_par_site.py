# fuel_tracking/management/commands/import_facturation_par_site.py
"""
Importe "ESCO SN _ base <mois> validé-GE.xlsx" (feuille "Facturation par
site") — livraison mensuelle Ops, la plus complète des 3 sources de
facturation/typologie (3301 sites vérifié 2026-09, contre 443 pour Base
août 26 et 469 pour Base GE.xlsx) et la SEULE à fournir directement les 2
colonnes demandées pour Suivis Consommation :
  - "Statut Facturation" (Oui/Non) → facturation_active_fichier. Daté du
    mois en cours (voir "Date Facture" en tête de feuille) — pas une
    moyenne annuelle comme conso_fichier_l, une vraie valeur du mois.
  - "Facturation avec GE oui|Non" → facturation_avec_ge_fichier, déjà
    calculée par Ops (combine statut facturation + présence GE), pas
    recalculée ici.
  - "Configuration v1" (Indoor/Outdoor) → configuration_fichier, 100%
    complet sur ce fichier (0 ligne vide, vérifié 2026-09) — écrase la
    valeur Base août 26 si les deux sont fournis pour le même site
    (source plus récente et plus complète).

Ne touche PAS à typology_fichier/typo_simple_fichier/site_type_fichier/
type_ge_fichier (Base GE.xlsx) ni conso_estimee_aout26_l/ge_runtime_aout26_h
(Base août 26) — uniquement les 3 champs ci-dessus.

Usage:
    docker compose exec web python manage.py import_facturation_par_site --file=data_imports/esco_facturation_sept26.xlsx --month=2026-09 --dry-run
    docker compose exec web python manage.py import_facturation_par_site --file=data_imports/esco_facturation_sept26.xlsx --month=2026-09
"""
import openpyxl
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelConsommationMonthly

SHEET_NAME = "Facturation par site"
HEADER_ROW = 7

# Indices 0-based dans chaque tuple de ligne
COL_SITE_ID = 0
COL_SITE_NAME = 1
COL_STATUT_FACTURATION = 5  # "Oui" / "Non"
COL_FACTURATION_AVEC_GE = 13  # "Oui" / "Non"
COL_CONFIGURATION = 16  # "Indoor" / "Outdoor"

FICHIER_SOURCE_LABEL = "ESCO SN — Facturation par site"


class Command(BaseCommand):
    help = 'Importe "ESCO SN _ base <mois> validé-GE.xlsx" (Facturation par site) — Statut Facturation/Facturation avec GE/Configuration.'

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
        self.stdout.write("  IMPORT FACTURATION PAR SITE (ESCO SN — Statut Facturation/Config Indoor-Outdoor)")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Fichier      : {path}")
        self.stdout.write(f"  Mois cible   : {month_year}")
        self.stdout.write(f"  Dry run      : {dry_run}")

        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        except FileNotFoundError:
            self.stdout.write(self.style.ERROR(f"\n  Fichier introuvable : {path}\n"))
            return

        ws = wb[SHEET_NAME]

        def to_bool_oui_non(v):
            if v is None:
                return None
            s = str(v).strip().lower()
            if s in ("oui", "yes", "true", "1"):
                return True
            if s in ("non", "no", "false", "0"):
                return False
            return None

        def to_str(v):
            return str(v).strip() if v is not None and str(v).strip() else None

        rows = []
        errors = []
        for i, row in enumerate(ws.iter_rows(min_row=HEADER_ROW + 1, values_only=True)):
            excel_row = HEADER_ROW + 1 + i
            site_id = row[COL_SITE_ID]
            if not site_id:
                continue
            site_id = str(site_id).strip()

            configuration = to_str(row[COL_CONFIGURATION])
            if configuration and configuration.lower() not in ("indoor", "outdoor"):
                errors.append({"row": excel_row, "site_id": site_id, "error": f"Configuration inattendue (ni Indoor ni Outdoor) : {configuration!r} — ignorée"})
                configuration = None
            elif configuration:
                configuration = configuration.capitalize()

            rows.append({
                "site_id": site_id,
                "site_name": to_str(row[COL_SITE_NAME]),
                "facturation_active_fichier": to_bool_oui_non(row[COL_STATUT_FACTURATION]),
                "facturation_avec_ge_fichier": to_bool_oui_non(row[COL_FACTURATION_AVEC_GE]),
                "configuration_fichier": configuration,
            })

        self.stdout.write(f"\n  Sites lus : {len(rows)}")
        if errors:
            self.stdout.write(self.style.WARNING(f"  Valeurs rejetées : {len(errors)}"))
            for e in errors[:20]:
                self.stdout.write(f"    ligne {e['row']} ({e['site_id']}) : {e['error']}")

        if dry_run:
            for r in rows[:10]:
                self.stdout.write(
                    f"    {r['site_id']} | facturation={r['facturation_active_fichier']} | "
                    f"facturation_avec_ge={r['facturation_avec_ge_fichier']} | configuration={r['configuration_fichier']}"
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
                set_field("facturation_active_fichier", r["facturation_active_fichier"])
                set_field("facturation_avec_ge_fichier", r["facturation_avec_ge_fichier"])
                set_field("configuration_fichier", r["configuration_fichier"])
                if update_fields:
                    fc.save(update_fields=update_fields)
                if is_created:
                    fcm_created += 1
                else:
                    fcm_updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"\n  FuelConsommationMonthly ({month_year}) : {fcm_created} créée(s), {fcm_updated} mise(s) à jour.\n"
        ))
