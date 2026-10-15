"""
Registre stock — traite les fichiers déposés dans FUEL_IMPORT_ROOT/incoming/{loliforce,scss,closing,modeles_cuves}
(même traitement que la tâche planifiée toutes les 15 minutes).

    python manage.py process_fuel_import_incoming
    python manage.py process_fuel_import_incoming --init   # crée seulement l'arborescence
"""
from django.core.management.base import BaseCommand

from fuel_tracking.services import stock_import_pipeline as P


class Command(BaseCommand):
    help = "Traite les fichiers du répertoire partagé du registre stock."

    def add_arguments(self, parser):
        parser.add_argument("--init", action="store_true")

    def handle(self, *args, init, **opts):
        r = P.ensure_dirs()
        self.stdout.write(f"Répertoire : {r}")
        if init:
            for p in sorted(x for x in r.rglob("*") if x.is_dir()):
                self.stdout.write(f"  {p}")
            return
        batches = P.scan_incoming()
        if not batches:
            self.stdout.write("Aucun fichier en attente dans incoming/.")
        for b in batches:
            self.stdout.write(f"  lot #{b.pk} {b.source_type} {b.file_name} → {b.status} : {b.status_message}")
