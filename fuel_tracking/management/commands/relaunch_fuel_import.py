"""
Registre stock — relance explicite et tracée d'un lot (retraite la copie d'archive ; nouveau lot lié).

    python manage.py relaunch_fuel_import --batch 12 --reason "Sites ajoutés dans Gestion des sites"
"""
from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.models import FuelImportBatch
from fuel_tracking.services import stock_import_pipeline as P


class Command(BaseCommand):
    help = "Relance un lot d'import du registre stock (motif obligatoire)."

    def add_arguments(self, parser):
        parser.add_argument("--batch", type=int, required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, batch, reason, **opts):
        b = FuelImportBatch.objects.filter(pk=batch).first()
        if b is None:
            raise CommandError(f"Lot #{batch} introuvable.")
        try:
            nb = P.relaunch(b, reason)
        except ValueError as e:
            raise CommandError(str(e))
        self.stdout.write(f"Lot #{nb.pk} (relance de #{b.pk}) → {nb.status} : {nb.status_message}")
