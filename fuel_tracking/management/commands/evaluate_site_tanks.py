"""
Registre stock — réévalue l'affectation des cuves site (statuts VALIDÉE / PROPOSÉE / À_CONFIRMER / INCOMPLÈTE /
PLUSIEURS_CUVES_À_IDENTIFIER) avec les règles Closing et propose les modèles de la bibliothèque (sans ambiguïté),
puis reconvertit les relevés des sites concernés. Les cuves validées ou rejetées par un humain ne sont jamais modifiées.

    python manage.py evaluate_site_tanks --dry-run
    python manage.py evaluate_site_tanks
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from fuel_tracking.models import FuelTankMaster
from fuel_tracking.services.stock_registry import reconvert_site_observations
from fuel_tracking.services.stock_tanks import evaluate_site_tanks


class Command(BaseCommand):
    help = "Réévalue les cuves site et reconvertit les relevés."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--sans-reconversion", action="store_true")

    def handle(self, *args, dry_run, sans_reconversion, **opts):
        with transaction.atomic():
            stats = evaluate_site_tanks(dry_run=dry_run)
            if not dry_run and not sans_reconversion:
                n = 0
                for sid in FuelTankMaster.objects.values_list("site_id", flat=True).distinct():
                    n += reconvert_site_observations(sid)
                stats["releves_reconvertis"] = n
        self.stdout.write(("DRY RUN — " if dry_run else "") + json.dumps(stats, ensure_ascii=False, indent=2))
