# fuel_tracking/management/commands/import_cph_abaque.py
"""
Importe l'abaque CPH GE PRP 50 Hz — voir services/cph_abaque_import.py.
Aussi disponible depuis l'onglet Contrôle CPH (Référentiel courbes → Importer l'abaque).

Usage :
    python manage.py import_cph_abaque --file data_imports/ABAQUE_CPH_GE_PRP_50HZ.xlsx [--dry-run]
"""
import os

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services.cph_abaque_import import apply_abaque, parse_abaque


class Command(BaseCommand):
    help = "Importe l'abaque CPH PRP 50 Hz (courbes + mappage inventaire)."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **o):
        path = o["file"]
        if not os.path.exists(path):
            raise CommandError(f"Fichier introuvable : {path}")
        try:
            parsed = parse_abaque(path)
        except ValueError as e:
            raise CommandError(str(e)) from e
        for w in parsed["warnings"]:
            self.stdout.write(self.style.WARNING(f"  {w}"))
        self.stdout.write(f"Courbes : {len(parsed['curves'])} {parsed['status_counts']}")
        self.stdout.write(f"Mappages : {len(parsed['mappings'])}")
        if o["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — rien écrit."))
            return
        for r in apply_abaque(parsed, os.path.basename(path)):
            self.stdout.write(self.style.WARNING(f"  Validation annulée (courbe plus candidate) : {r}"))
        self.stdout.write(self.style.SUCCESS(f"Abaque importé depuis {os.path.basename(path)}."))
