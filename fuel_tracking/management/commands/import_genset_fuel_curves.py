# fuel_tracking/management/commands/import_genset_fuel_curves.py
"""
Importe le catalogue de courbes de consommation GE (feuille "GENSET DB" du
fichier de synthèse Ops) dans GensetFuelCurve.

Chaque ligne du catalogue donne, pour un modèle de GE donné : la conso
mesurée à 100/75/50% de charge (L/h) et les coefficients a/b/c de la
régression quadratique ajustée sur ces 3 points (conso(x) = a·x² + b·x + c).

Usage:
    docker compose exec web python manage.py import_genset_fuel_curves
    docker compose exec web python manage.py import_genset_fuel_curves --dry-run
"""
from pyxlsb import open_workbook
from django.core.management.base import BaseCommand
from django.utils import timezone

from fuel_tracking.models import GensetFuelCurve


XLSB_PATH = "data_imports/esco_synthese_conso_fuel_juin2026.xlsb"
XLSB_SHEET = "GENSET DB"
HEADER_ROW = 1  # 0-indexed


class Command(BaseCommand):
    help = "Importe le catalogue de courbes de consommation GE depuis GENSET DB (xlsb)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT CATALOGUE COURBES CONSO GE")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Lecture : {XLSB_PATH} / {XLSB_SHEET}")

        with open_workbook(XLSB_PATH) as wb:
            with wb.get_sheet(XLSB_SHEET) as sh:
                rows = list(sh.rows())

        header = [c.v for c in rows[HEADER_ROW]]

        def col(name):
            return header.index(name)

        i_manufacturer = col("Manufacturer ")
        i_type_de_ge = col("Type de GE")
        i_genset_list = col("Genset List")
        i_voltage = col("Voltage [V]")
        i_phases = col("No. of phases")
        i_prp_kva = col("PRP [kVA]")
        i_prp_kw = col("PRP [kW]")
        i_cosphi = col("cosφ")
        i_c100 = col("Conso 100%")
        i_c75 = col("Conso 75%")
        i_c50 = col("Conso 50%")
        i_a = col("a")
        i_b = col("b")
        i_c = col("c")

        objects = []
        skipped = 0

        for row in rows[HEADER_ROW + 2:]:
            v = [c.v for c in row]
            if len(v) <= i_c:
                continue

            type_de_ge = v[i_type_de_ge]
            manufacturer = v[i_manufacturer]

            if not type_de_ge or v[i_prp_kva] is None or v[i_c100] is None:
                skipped += 1
                continue

            objects.append(
                GensetFuelCurve(
                    manufacturer=str(manufacturer or "").strip(),
                    manufacturer_normalized=str(manufacturer or "").strip().upper(),
                    type_de_ge=str(type_de_ge).strip(),
                    genset_list=str(v[i_genset_list]).strip() if v[i_genset_list] else None,
                    voltage_v=v[i_voltage],
                    phases=int(v[i_phases]) if v[i_phases] else None,
                    prp_kva=v[i_prp_kva],
                    prp_kw=v[i_prp_kw],
                    cosphi=v[i_cosphi],
                    conso_100_l_h=v[i_c100],
                    conso_75_l_h=v[i_c75],
                    conso_50_l_h=v[i_c50],
                    coef_a=v[i_a] or 0,
                    coef_b=v[i_b] or 0,
                    coef_c=v[i_c] or 0,
                    imported_at=timezone.now(),
                )
            )

        self.stdout.write(f"  Modèles lus     : {len(objects)}")
        self.stdout.write(f"  Lignes ignorées : {skipped} (données incomplètes)")

        if dry_run:
            for o in objects[:10]:
                self.stdout.write(f"    {o.manufacturer} | {o.type_de_ge} | {o.prp_kva} kVA | "
                                   f"a={o.coef_a:.3f} b={o.coef_b:.3f} c={o.coef_c:.3f}")
            self.stdout.write(self.style.WARNING("\n  DRY RUN — aucune donnée écrite.\n"))
            return

        GensetFuelCurve.objects.all().delete()
        GensetFuelCurve.objects.bulk_create(objects, batch_size=500)

        self.stdout.write(self.style.SUCCESS(f"\n  {len(objects)} courbes importées dans GensetFuelCurve.\n"))
