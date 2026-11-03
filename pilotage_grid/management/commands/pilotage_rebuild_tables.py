"""Reconstruit les tables relationnelles Pilotage Grid depuis les jeux actifs (après migration, ou pour resynchroniser)."""
from django.core.management.base import BaseCommand

from pilotage_grid.services import tables


class Command(BaseCommand):
    help = "Reconstruit les tables relationnelles Pilotage Grid."

    def handle(self, *args, **opts):
        self.stdout.write(str(tables.rebuild_all()))
