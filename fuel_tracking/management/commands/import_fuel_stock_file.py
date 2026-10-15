"""
Registre stock — importe un fichier (Loliforce, SCSS, Closing, historique refueling) hors répertoire monté.
Le fichier source n'est ni modifié ni déplacé : une copie d'archive est écrite dans FUEL_IMPORT_ROOT/archive.

    python manage.py import_fuel_stock_file --type LOLIFORCE --file data_imports/Template_remontees_Loliforce.xlsx
    python manage.py import_fuel_stock_file --type SCSS --file data_imports/REMONTE_CARBURANT_OCTOBRE_2026.xlsx
    python manage.py import_fuel_stock_file --type CLOSING --file data_imports/Synthese_Conso_Fuel_Aout_2026.xlsb
    python manage.py import_fuel_stock_file --type HISTORICAL_REFUELING --file data_imports/Suivi_refueling_Septembre.xlsx
    python manage.py import_fuel_stock_file --type TANK_MODELS --file data_imports/Senegal_Tank_DB_Calculateur.xlsx
    python manage.py import_fuel_stock_file ... --dry-run   # contrôles et bilan, rien n'est enregistré
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel_tracking.services import stock_import_pipeline as P


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Importe un fichier dans le registre stock carburant."

    def add_arguments(self, parser):
        parser.add_argument("--type", required=True, choices=["LOLIFORCE", "SCSS", "CLOSING", "HISTORICAL_REFUELING", "TANK_MODELS"])
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, type, file, dry_run, **opts):
        path = Path(file)
        if not path.is_file():
            raise CommandError(f"Fichier introuvable : {path}")
        data = path.read_bytes()
        result = {}
        try:
            with transaction.atomic():
                batch = P.process_bytes(data, path.name, type, origin="COMMAND", move_files=not dry_run)
                result = _report(batch)
                if dry_run:
                    raise _DryRun()
        except _DryRun:
            self.stdout.write("DRY RUN — aucune donnée enregistrée.")
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _report(batch):
    return {
        "lot": batch.pk, "type": batch.source_type, "fichier": batch.file_name, "statut": batch.status,
        "message": batch.status_message, "lignes": batch.rows_total, "acceptees": batch.rows_accepted,
        "alertes": batch.rows_warning, "a_valider": batch.rows_pending, "rejetees": batch.rows_rejected,
        "doublons": batch.rows_duplicate, "bilan": batch.summary, "rapport": batch.report_path, "archive": batch.archive_path,
    }
