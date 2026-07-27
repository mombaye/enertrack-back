# certification/management/commands/diagnose_snowflake_efms.py
"""
Diagnostique SnowflakeEfmsService et compare ses résultats à EfmsService
(SQL Server) pour un ou plusieurs sites, sur une période donnée.

Usage:
    docker compose exec web python manage.py diagnose_snowflake_efms --site-id DKR_0059
    docker compose exec web python manage.py diagnose_snowflake_efms --site-id DKR_0059 --site-id ORG_2201 --days 30
"""
import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService
from certification.services.snowflake_efms import SnowflakeEfmsService


class Command(BaseCommand):
    help = "Diagnostique SnowflakeEfmsService et compare à EfmsService (SQL Server)"

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

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  COMPARAISON SQL Server (EfmsService) vs Snowflake (SnowflakeEfmsService)")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Période : {date_debut} → {date_fin}\n")

        sf_svc = SnowflakeEfmsService()
        self.stdout.write(f"  Connexion Snowflake : {'OK' if sf_svc.check_connection() else 'ÉCHEC'}")

        sql_svc = EfmsService()
        sql_ok = sql_svc.check_connection()
        self.stdout.write(f"  Connexion SQL Server : {'OK' if sql_ok else 'ÉCHEC (tunnel down ?)'}\n")

        for site_id in site_ids:
            self.stdout.write(f"  ── {site_id} " + "─" * (60 - len(site_id)))

            try:
                sf_conso, sf_method = sf_svc.get_conso_periode(site_id, date_debut, date_fin)
            except Exception as e:
                sf_conso, sf_method = None, f"ERREUR: {e}"

            sql_conso, sql_method = None, "non testé (SQL down)"
            if sql_ok:
                try:
                    sql_conso, sql_method = sql_svc.get_conso_periode(site_id, date_debut, date_fin)
                except Exception as e:
                    sql_conso, sql_method = None, f"ERREUR: {e}"

            self.stdout.write(f"    SQL Server : {sql_conso} kWh [{sql_method}]")
            self.stdout.write(f"    Snowflake  : {sf_conso} kWh [{sf_method}]")

            if sql_conso is not None and sf_conso is not None:
                gap = abs(float(sql_conso) - float(sf_conso))
                gap_pct = (gap / float(sql_conso) * 100) if sql_conso else 0
                tone = self.style.SUCCESS if gap_pct < 5 else self.style.WARNING
                self.stdout.write(tone(f"    Écart      : {gap:.2f} kWh ({gap_pct:.1f}%)"))
            self.stdout.write("")

        sys.exit(0)
