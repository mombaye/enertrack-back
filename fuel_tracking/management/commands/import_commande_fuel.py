# fuel_tracking/management/commands/import_commande_fuel.py
"""
Importe "Commande FUEL ESCO SENEGAL <mois>.xlsb" — 2 feuilles, import brut
sans recalcul (chaque valeur reprise telle quelle, mois courant/précédent/
écart déjà calculés dans le fichier source) :

  - "Synthèse Commande" -> FuelCommandeSynthese, 2 blocs empilés dans la
    même feuille (repérés par leur ligne d'en-tête "CATEGORIE" / "Typologie
    facturée") : par catégorie/batch, et par typologie facturée.
  - "Suivis commande"   -> FuelSuiviCommandeSite, 12 colonnes retenues sur
    136 (celles considérées importantes par l'équipe Ops — voir le
    docstring du modèle) : identité/typologie du site + les 4 valeurs de
    décision (conso moyenne, commande sans/avec marge, stock final estimé).

Chaque mois remplace entièrement ses lignes (pas d'upsert ligne à ligne :
aucune clé naturelle stable côté FuelCommandeSynthese, structure du fichier
mensuel jetable comme FuelConsommationMonthly.sync_fuel_consommation).

Usage:
    docker compose exec web python manage.py import_commande_fuel --file=data_imports/commande_fuel_aout_2026.xlsb --month=2026-08 --dry-run
    docker compose exec web python manage.py import_commande_fuel --file=data_imports/commande_fuel_aout_2026.xlsb --month=2026-08
"""
import re
from decimal import Decimal, InvalidOperation

import pandas as pd
from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite

SITE_ID_RE = re.compile(r"^[A-Z]{3}_\d{4}$")

SYNTHESE_SHEET = "Synthèse Commande"
SUIVI_SHEET = "Suivis commande"
SUIVI_DATA_START = 13  # 0-indexed — ligne d'en-tête réelle à la ligne 12

# Indices de colonnes 0-based (feuille "Suivis commande", 136 colonnes au
# total) — les seules retenues, correspondance vérifiée sur le fichier
# Août 2026 (colonnes 85/87/105 nomment explicitement "Juillet"/"Août 2026").
SUIVI_COLS = {
    2: "site_id", 3: "site_name", 4: "typologie_contractuelle", 5: "load_commande",
    6: "indoor_outdoor", 8: "longitude", 9: "batch", 12: "typologie_facturee",
    79: "conso_moy_jour_l", 85: "commande_sans_marge_l", 87: "commande_avec_marge_l",
    105: "estimation_stock_final_l", 132: "typo_operations",
}


def _dec(v):
    if v is None:
        return Decimal("0")
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return d if d.is_finite() else Decimal("0")


def _str(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:  # NaN != NaN
        return None
    s = str(v).strip()
    return s or None


def _prev_month(month_year):
    year, month = (int(x) for x in month_year.split("-"))
    return f"{year - 1:04d}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


class Command(BaseCommand):
    help = 'Importe "Commande FUEL ESCO SENEGAL <mois>.xlsb" (Synthèse Commande + Suivis commande).'

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--month", required=True, help="YYYY-MM — mois courant du fichier (celui de la commande décidée).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        month_year = options["month"]
        dry_run = options["dry_run"]
        prev_month_year = _prev_month(month_year)

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT COMMANDE FUEL")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Fichier    : {path}")
        self.stdout.write(f"  Mois       : {month_year} (précédent : {prev_month_year})")
        self.stdout.write(f"  Dry run    : {dry_run}")

        synth_rows = self._read_synthese(path)
        suivi_rows = self._read_suivi(path)

        self.stdout.write(f"\n  Synthèse Commande lue : {len(synth_rows)} ligne(s)")
        self.stdout.write(f"  Suivis commande lus   : {len(suivi_rows)} site(s)")

        if dry_run:
            self.stdout.write("\n  Aperçu Synthèse Commande :")
            for r in synth_rows[:10]:
                self.stdout.write(f"    [{r['group_type']:10}] {r['label']:28} total={r['total_l']:>10} (préc. {r['total_prev_l']:>10})")
            self.stdout.write("\n  Aperçu Suivis commande :")
            for r in suivi_rows[:5]:
                self.stdout.write(f"    {r['site_id']} | {r['site_name']} | commande avec marge={r['commande_avec_marge_l']} L | stock final estimé={r['estimation_stock_final_l']} L")
            self.stdout.write(self.style.WARNING(f"\n  DRY RUN — aucune donnée écrite ({len(synth_rows)} ligne(s) Synthèse, {len(suivi_rows)} site(s) Suivi prêts).\n"))
            return

        with transaction.atomic():
            FuelCommandeSynthese.objects.filter(month_year=month_year).delete()
            FuelCommandeSynthese.objects.bulk_create([
                FuelCommandeSynthese(month_year=month_year, prev_month_year=prev_month_year, **r)
                for r in synth_rows
            ])

            FuelSuiviCommandeSite.objects.filter(month_year=month_year).delete()
            FuelSuiviCommandeSite.objects.bulk_create([
                FuelSuiviCommandeSite(month_year=month_year, source_filename=path.rsplit("/", 1)[-1], **r)
                for r in suivi_rows
            ], batch_size=1000)

        self.stdout.write(self.style.SUCCESS(
            f"\n  {len(synth_rows)} ligne(s) Synthèse Commande + {len(suivi_rows)} site(s) Suivi commande importés pour {month_year}.\n"
        ))

    def _read_synthese(self, path):
        df = pd.read_excel(path, sheet_name=SYNTHESE_SHEET, engine="pyxlsb", header=None)
        rows = []
        group_type = None
        order_index = 0
        for i in range(len(df)):
            row = df.iloc[i].tolist()
            label = row[1] if len(row) > 1 else None
            if isinstance(label, str) and label.strip() == "CATEGORIE":
                group_type, order_index = FuelCommandeSynthese.GroupType.CATEGORIE, 0
                continue
            if isinstance(label, str) and label.strip() == "Typologie facturée":
                group_type, order_index = FuelCommandeSynthese.GroupType.TYPOLOGIE, 0
                continue
            if group_type is None or not isinstance(label, str) or not label.strip():
                continue
            label = label.strip()
            order_index += 1
            rows.append({
                "group_type": group_type,
                "order_index": order_index,
                "label": label,
                "is_total_row": "TOTAL" in label.upper(),
                "nb_sites": _dec(row[2]),
                "commande_normale_l": _dec(row[3]),
                "commande_hivernale_l": _dec(row[4]),
                "total_l": _dec(row[5]),
                "nb_sites_prev": _dec(row[6]),
                "commande_normale_prev_l": _dec(row[7]),
                "commande_hivernale_prev_l": _dec(row[8]),
                "total_prev_l": _dec(row[9]),
                "ecart_sites": _dec(row[10]),
                "ecart_qte_l": _dec(row[11]),
                "commentaires": _str(row[13]) if len(row) > 13 else None,
            })
        return rows

    def _read_suivi(self, path):
        df = pd.read_excel(path, sheet_name=SUIVI_SHEET, engine="pyxlsb", header=None, skiprows=SUIVI_DATA_START)
        rows = []
        for i in range(len(df)):
            row = df.iloc[i]
            site_id = row.get(2)
            if not isinstance(site_id, str) or not SITE_ID_RE.match(site_id.strip()):
                continue
            lon = row.get(8)
            rows.append({
                "site_id": site_id.strip(),
                "site_name": _str(row.get(3)),
                "typologie_contractuelle": _str(row.get(4)),
                "load_commande": _dec(row.get(5)),
                "indoor_outdoor": _str(row.get(6)),
                "longitude": float(lon) if isinstance(lon, (int, float)) else None,
                "batch": _str(row.get(9)),
                "typologie_facturee": _str(row.get(12)),
                "conso_moy_jour_l": _dec(row.get(79)),
                "commande_sans_marge_l": _dec(row.get(85)),
                "commande_avec_marge_l": _dec(row.get(87)),
                "estimation_stock_final_l": _dec(row.get(105)),
                "typo_operations": _str(row.get(132)),
            })
        return rows
