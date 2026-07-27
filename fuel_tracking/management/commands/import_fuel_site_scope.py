# fuel_tracking/management/commands/import_fuel_site_scope.py
"""
Peuple FuelSiteScope : le périmètre des sites réellement concernés par le
suivi fuel (sites avec GE installé), à partir de deux fichiers de référence
utilisés par l'équipe Ops :

1. data_imports/suivi_ravitaillement_11062026.xlsx, feuille "Conso Monthly"
   → liste curée de ~525 sites avec un flag "GE Exist" (OUI/NON/A VÉRIFIER)
     explicite par site. Source la plus fiable (audit terrain).

2. data_imports/esco_synthese_conso_fuel_juin2026.xlsb, feuille
   "Synthese conso fuel" → couvre TOUT le parc (~3300 sites), avec pour
   chaque site un couple (Typologie Facturée, Typologi Catalogue Installée).
   On en dérive un crosswalk code → catégorie, utilisé en repli pour les
   sites absents de la liste curée (catégories avec GE : HS- SX, HGB,
   GG-WG, GG-WG-SO — validé sur les données réelles, ~437 sites).

La liste curée est prioritaire (source de vérité audit terrain) ; le
crosswalk typologie comble les sites absents de cette liste, ce qui permet
au périmètre de rester correct même pour des sites ajoutés/modifiés après
la date de cet export.

Usage:
    docker compose exec web python manage.py import_fuel_site_scope
    docker compose exec web python manage.py import_fuel_site_scope --dry-run
"""
from collections import defaultdict, Counter

import openpyxl
from pyxlsb import open_workbook
from django.core.management.base import BaseCommand
from django.utils import timezone

from fuel_tracking.models import FuelSiteScope


CURATED_PATH = "data_imports/suivi_ravitaillement_11062026.xlsx"
CURATED_SHEET = "Conso Monthly"
CURATED_HEADER_ROW = 4  # 0-indexed

XLSB_PATH = "data_imports/esco_synthese_conso_fuel_juin2026.xlsb"
XLSB_SHEET = "Synthese conso fuel"
XLSB_HEADER_ROW = 14  # 0-indexed

# Catégories "Typologi Catalogue Installée" qui impliquent un GE installé,
# confirmées sur les données réelles (voir docstring module).
GENSET_CATALOGUE_CATEGORIES = {"HS- SX", "HGB", "GG-WG", "GG-WG-SO", "CSN", "MG-WG", "BG-WG", "BG-WG-SO", "MG-WG-SO"}

GE_EXIST_TRUE = {"OUI", "YES", "TRUE"}
GE_EXIST_FALSE = {"NON", "NO", "FALSE"}


class Command(BaseCommand):
    help = "Importe le périmètre fuel (sites avec GE) depuis les fichiers de référence Ops"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def load_curated(self):
        """Retourne {site_id: (has_genset, site_name, billing_typology)} depuis la liste Ops curée."""
        wb = openpyxl.load_workbook(CURATED_PATH, read_only=True, data_only=True)
        ws = wb[CURATED_SHEET]
        rows = list(ws.iter_rows(values_only=True))
        header = [str(h).strip() if h else "" for h in rows[CURATED_HEADER_ROW]]

        def col(name):
            return header.index(name)

        i_site = col("Site ID")
        i_name = col("Site Name")
        i_typo_facturee = col("Typologie facturée")
        i_ge_exist = col("GE Exist")

        result = {}
        ge_exist_counts = Counter()

        for row in rows[CURATED_HEADER_ROW + 1:]:
            site_id = row[i_site]
            if not site_id:
                continue

            site_id = str(site_id).strip()
            ge_exist_raw = str(row[i_ge_exist] or "").strip().upper()
            ge_exist_counts[ge_exist_raw] += 1

            if ge_exist_raw in GE_EXIST_TRUE:
                has_genset = True
            elif ge_exist_raw in GE_EXIST_FALSE:
                has_genset = False
            else:
                # "A VÉRIFIER" ou vide : on garde le site dans le périmètre
                # (mieux vaut suivre un site en trop que rater un site avec GE).
                has_genset = True

            result[site_id] = (
                has_genset,
                str(row[i_name]).strip() if row[i_name] else None,
                str(row[i_typo_facturee]).strip() if row[i_typo_facturee] else None,
            )

        wb.close()
        return result, ge_exist_counts

    def load_typology_crosswalk_and_full_park(self):
        """
        Retourne :
        - crosswalk {billing_typology_code: catalogue_category} le plus fréquent par code
        - full_park {site_id: (has_genset, site_name, billing_typology, catalogue_category)}
          pour TOUT le parc présent dans le fichier xlsb.
        """
        code_to_catalogue = defaultdict(Counter)
        full_park = {}

        with open_workbook(XLSB_PATH) as wb:
            with wb.get_sheet(XLSB_SHEET) as sh:
                rows = list(sh.rows())

        header_row = [c.v for c in rows[XLSB_HEADER_ROW]]
        i_site = header_row.index("ID du site")
        i_name = header_row.index("Nom du site")
        i_typo_facturee = header_row.index("Typologie Facturée")
        i_catalogue = header_row.index("Typologi Catalogue Installée")

        for row in rows[XLSB_HEADER_ROW + 1:]:
            v = [c.v for c in row]
            if len(v) <= i_catalogue:
                continue

            site_id = v[i_site]
            if not site_id:
                continue

            site_id = str(site_id).strip()
            typo_facturee = v[i_typo_facturee]
            catalogue = v[i_catalogue]

            if typo_facturee and catalogue:
                code_to_catalogue[str(typo_facturee).strip()][str(catalogue).strip()] += 1

            has_genset = bool(catalogue) and str(catalogue).strip() in GENSET_CATALOGUE_CATEGORIES

            full_park[site_id] = (
                has_genset,
                str(v[i_name]).strip() if v[i_name] else None,
                str(typo_facturee).strip() if typo_facturee else None,
                str(catalogue).strip() if catalogue else None,
            )

        crosswalk = {code: counter.most_common(1)[0][0] for code, counter in code_to_catalogue.items()}
        return crosswalk, full_park

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT PÉRIMÈTRE FUEL (sites avec GE)")
        self.stdout.write("═" * 80)

        self.stdout.write(f"  Lecture liste curée : {CURATED_PATH}")
        curated, ge_exist_counts = self.load_curated()
        self.stdout.write(f"    → {len(curated)} sites, GE Exist = {dict(ge_exist_counts)}")

        self.stdout.write(f"  Lecture crosswalk typologie (parc complet) : {XLSB_PATH}")
        crosswalk, full_park = self.load_typology_crosswalk_and_full_park()
        self.stdout.write(f"    → {len(full_park)} sites au total dans le parc")
        self.stdout.write(f"    → {len(crosswalk)} codes de typologie distincts")

        # Fusion : la liste curée est prioritaire, le crosswalk comble les sites absents.
        merged = {}

        for site_id, (has_genset, name, typo, catalogue) in full_park.items():
            merged[site_id] = {
                "has_genset": has_genset,
                "site_name": name,
                "billing_typology": typo,
                "catalogue_typology": catalogue,
                "source": FuelSiteScope.Source.TYPOLOGY_CROSSWALK,
            }

        for site_id, (has_genset, name, typo) in curated.items():
            existing = merged.get(site_id, {})
            merged[site_id] = {
                "has_genset": has_genset,
                "site_name": name or existing.get("site_name"),
                "billing_typology": typo or existing.get("billing_typology"),
                "catalogue_typology": existing.get("catalogue_typology"),
                "source": FuelSiteScope.Source.CURATED_OPS_LIST,
            }

        with_genset = [s for s in merged.values() if s["has_genset"]]
        by_source = Counter(s["source"] for s in with_genset)

        self.stdout.write("\n  ── Résultat fusionné ──")
        self.stdout.write(f"  Total sites référencés     : {len(merged)}")
        self.stdout.write(self.style.SUCCESS(f"  Sites avec GE (has_genset)  : {len(with_genset)}"))
        self.stdout.write(f"    dont liste curée (OUI)   : {by_source.get(FuelSiteScope.Source.CURATED_OPS_LIST, 0)}")
        self.stdout.write(f"    dont crosswalk typologie : {by_source.get(FuelSiteScope.Source.TYPOLOGY_CROSSWALK, 0)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  DRY RUN — aucune donnée écrite.\n"))
            return

        objects = [
            FuelSiteScope(
                site_id=site_id,
                site_name=data["site_name"],
                has_genset=data["has_genset"],
                catalogue_typology=data["catalogue_typology"],
                billing_typology=data["billing_typology"],
                source=data["source"],
                imported_at=timezone.now(),
            )
            for site_id, data in merged.items()
        ]

        FuelSiteScope.objects.all().delete()
        FuelSiteScope.objects.bulk_create(objects, batch_size=1000)

        self.stdout.write(self.style.SUCCESS(f"\n  {len(objects)} sites importés dans FuelSiteScope.\n"))
