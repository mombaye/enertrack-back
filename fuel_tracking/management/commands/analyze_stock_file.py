"""
Analyse préalable d'un fichier de stock terrain (LECTURE SEULE, aucun import) : feuilles, colonnes, mapping, couverture de sites.

    python manage.py analyze_stock_file /mnt/Suivi_carburant_prod/incoming/loliforce/fichier.xlsx --source LOLIFORCE
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services.stock_file_analysis import analyze


class Command(BaseCommand):
    help = "Analyse un fichier de stock (feuilles, colonnes, mapping) sans rien importer."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--source", default=None, help="LOLIFORCE, SCSS, CLOSING, HISTORICAL_REFUELING")

    def handle(self, *args, path, source, **opts):
        f = Path(path)
        if not f.is_file():
            raise CommandError(f"Fichier introuvable : {path}")
        self.stdout.write(json.dumps(analyze(f.read_bytes(), f.name, source), ensure_ascii=False, indent=2, default=str))
