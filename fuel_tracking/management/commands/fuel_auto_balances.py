"""Calcule les bilans de stock des N derniers mois complets (même traitement que la tâche planifiée nocturne). Idempotent.

    python manage.py fuel_auto_balances [--mois 2]
"""
from django.core.management.base import BaseCommand

from fuel_tracking.services.auto_balances import compute_recent_months


class Command(BaseCommand):
    help = "Calcule les bilans de stock des derniers mois complets."

    def add_arguments(self, parser):
        parser.add_argument("--mois", type=int, default=2)

    def handle(self, *args, mois, **opts):
        for r in compute_recent_months(mois):
            self.stdout.write(f"{r['debut']} → {r['fin']} : {r['sites']} sites, {r['calculables']} calculables")
