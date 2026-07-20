# fuel_tracking/management/commands/diagnose_rh.py
"""
Diagnostique le calcul RH (cascade Snowflake -> ENOC) pour un ou plusieurs sites.

Usage:
    docker compose exec web python manage.py diagnose_rh --site-id DKR_0059
    docker compose exec web python manage.py diagnose_rh --site-id DKR_0059 --site-id KDG_2385 --days 30
    docker compose exec web python manage.py diagnose_rh --site-id DKR_0059 --date-debut 2026-06-01 --date-fin 2026-06-30
"""
import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from fuel_tracking.services.rh_service import RhCalculationService


class Command(BaseCommand):
    help = "Diagnostique le calcul RH (Snowflake prioritaire, ENOC en secours)"

    def add_arguments(self, parser):
        parser.add_argument("--site-id", action="append", required=True, dest="site_ids")
        parser.add_argument("--days", type=int, default=30)
        parser.add_argument("--date-debut", type=str, default=None)
        parser.add_argument("--date-fin", type=str, default=None)

    def handle(self, *args, **options):
        site_ids = options["site_ids"]

        if options["date_debut"] and options["date_fin"]:
            date_debut = date.fromisoformat(options["date_debut"])
            date_fin = date.fromisoformat(options["date_fin"])
        else:
            date_fin = date.today()
            date_debut = date_fin - timedelta(days=options["days"] - 1)

        self.stdout.write("\n" + "═" * 70)
        self.stdout.write("  DIAGNOSTIC RH — cascade Snowflake / ENOC")
        self.stdout.write("═" * 70)
        self.stdout.write(f"  Période : {date_debut} → {date_fin}")
        self.stdout.write(f"  Sites   : {', '.join(site_ids)}\n")

        try:
            svc = RhCalculationService()
            results = svc.compute_rh_batch(site_ids, date_debut, date_fin)
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Erreur : {e}"))
            sys.exit(1)

        for site_id in site_ids:
            r = results.get(site_id, {})
            rh = r.get("rh_hours")
            source = r.get("source")
            avec_dse = r.get("avec_dse")

            self.stdout.write(f"  {site_id}")
            self.stdout.write(f"    avec DSE : {avec_dse}")
            if rh is not None:
                self.stdout.write(self.style.SUCCESS(f"    RH       : {rh:.2f} h  [{source}]"))
            else:
                self.stdout.write(self.style.WARNING(f"    RH       : aucune donnée [{source}]"))
            self.stdout.write("")

        self.stdout.write("═" * 70 + "\n")
