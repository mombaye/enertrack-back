# fuel_tracking/management/commands/import_cph_matrix.py
"""
Importe la matrice CPH (feuille "CPH" du fichier Suivi Ravitaillement) dans
CphMatrixPoint.

La feuille est organisée en "bandes" : une ligne de titre moteur ("Moteur
Perkins", "Moteur Kohler"...), suivie d'une ligne d'en-tête ("DGCapacity in
KVA" + 20 paliers de % de charge de 0 à 1), suivie de 9 lignes de données
(une par taille de moteur en kVA : 10/12/15/20/30/40/50/60/80). Deux blocs
moteur sont posés côte à côte par bande (ex: Perkins à gauche, Kohler à
droite), et il y a plusieurs bandes empilées verticalement.

On scanne toutes les cellules à la recherche du libellé "DGCapacity in KVA"
pour détecter chaque bloc automatiquement (robuste au nombre de bandes/blocs,
pas de dépendance à des indices de ligne/colonne codés en dur). Les blocs
sans aucune donnée (moteur pas encore renseigné, ex: Kohler/Mitsubishi/"…..")
sont ignorés.

Usage:
    docker compose exec web python manage.py import_cph_matrix
    docker compose exec web python manage.py import_cph_matrix --dry-run
"""
import openpyxl
from django.core.management.base import BaseCommand
from django.utils import timezone

from fuel_tracking.models import CphMatrixPoint


XLSX_PATH = "data_imports/suivi_ravitaillement_11062026.xlsx"
SHEET_NAME = "CPH"
HEADER_LABEL = "DGCapacity in KVA"
KVA_ROWS = 9  # 10, 12, 15, 20, 30, 40, 50, 60, 80 kVA


class Command(BaseCommand):
    help = "Importe la matrice CPH (conso L/h par % de charge) depuis Suivi Ravitaillement"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT MATRICE CPH")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Lecture : {XLSX_PATH} / {SHEET_NAME}")

        wb = openpyxl.load_workbook(XLSX_PATH, read_only=True, data_only=True)
        ws = wb[SHEET_NAME]
        grid = [list(row) for row in ws.iter_rows(values_only=True)]
        wb.close()

        blocks_found = 0
        blocks_empty = 0
        objects = []

        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                if cell != HEADER_LABEL:
                    continue

                blocks_found += 1
                engine_name = grid[r - 1][c] if r > 0 else None
                engine_name = str(engine_name).strip() if engine_name else f"Moteur inconnu (L{r}C{c})"

                charge_steps = row[c + 1: c + 1 + 20]
                data_rows = grid[r + 1: r + 1 + KVA_ROWS]

                has_data = any(
                    drow[c + 1 + i] is not None
                    for drow in data_rows
                    for i in range(len(charge_steps))
                    if drow[c] is not None
                )

                if not has_data:
                    blocks_empty += 1
                    self.stdout.write(f"    (bloc vide ignoré : {engine_name})")
                    continue

                for drow in data_rows:
                    kva = drow[c]
                    if kva is None:
                        continue
                    for i, pct in enumerate(charge_steps):
                        if pct is None:
                            continue
                        value = drow[c + 1 + i]
                        if value is None:
                            continue
                        objects.append(
                            CphMatrixPoint(
                                engine_family=engine_name,
                                engine_family_normalized=engine_name.strip().upper(),
                                dg_capacity_kva=kva,
                                charge_pct=pct,
                                cph_l_h=value,
                                imported_at=timezone.now(),
                            )
                        )

                self.stdout.write(self.style.SUCCESS(f"    bloc importé : {engine_name} ({len(data_rows)} tailles kVA)"))

        self.stdout.write(f"\n  Blocs détectés : {blocks_found} ({blocks_empty} vides ignorés)")
        self.stdout.write(f"  Points de mesure : {len(objects)}")

        if dry_run:
            for o in objects[:10]:
                self.stdout.write(f"    {o.engine_family} | {o.dg_capacity_kva} kVA | {float(o.charge_pct):.0%} -> {o.cph_l_h} L/h")
            self.stdout.write(self.style.WARNING("\n  DRY RUN — aucune donnée écrite.\n"))
            return

        CphMatrixPoint.objects.all().delete()
        CphMatrixPoint.objects.bulk_create(objects, batch_size=1000)

        self.stdout.write(self.style.SUCCESS(f"\n  {len(objects)} points importés dans CphMatrixPoint.\n"))
