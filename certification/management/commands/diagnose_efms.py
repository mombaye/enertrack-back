# certification/management/commands/diagnose_efms.py
"""
Management command pour diagnostiquer la connexion eFMS.
Usage:
    docker compose exec web python manage.py diagnose_efms
    docker compose exec web python manage.py diagnose_efms --site-id CAM-DKR-001
"""
import json
import socket
import sys

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService


class Command(BaseCommand):
    help = "Diagnostique la connexion SQL Server eFMS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--site-id", type=str, default=None,
            help="Tester une requête métier sur ce site_id"
        )

    def handle(self, *args, **options):
        self.stdout.write("\n" + "═" * 60)
        self.stdout.write("  DIAGNOSTIC eFMS SQL SERVER")
        self.stdout.write("═" * 60 + "\n")

        efms = EfmsService()
        result = efms.diagnose()

        def ok(v):
            return self.style.SUCCESS("✓ OK") if v else self.style.ERROR("✗ FAIL")

        self.stdout.write(f"  Hôte         : {result['host']}:{result['port']}")
        self.stdout.write(f"  Base         : {result['db']}")
        self.stdout.write(f"  Utilisateur  : {result['user']}")
        self.stdout.write(f"  Driver       : {result['driver']}")
        self.stdout.write("")
        self.stdout.write(f"  TCP (port 1433)    : {ok(result['tcp_reachable'])}")
        self.stdout.write(f"  Connexion ODBC     : {ok(result['odbc_connected'])}")
        self.stdout.write(f"  Requête SELECT 1   : {ok(result['query_ok'])}")

        if result["row_count_test"] is not None:
            self.stdout.write(f"  Table Grid (TOP 1) : {self.style.SUCCESS(str(result['row_count_test']))}")

        if result["error"]:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(f"  ERREUR : {result['error']}"))

        # Test requête métier si site fourni
        site_id = options.get("site_id")
        if site_id and result["query_ok"]:
            self.stdout.write("")
            self.stdout.write(f"  Test métier pour site_id={site_id!r} ...")
            from datetime import date, timedelta
            end = date.today()
            start = end - timedelta(days=30)
            try:
                conso = efms.get_conso_periode(site_id, start, end)
                self.stdout.write(
                    f"  get_conso_periode({start} → {end}) : "
                    + (self.style.SUCCESS(f"{conso} kWh") if conso else self.style.WARNING("None (aucune donnée)"))
                )
                conso_m, ref_m = efms.get_conso_last_complete_month(site_id, end)
                self.stdout.write(
                    f"  get_conso_last_complete_month     : "
                    + (self.style.SUCCESS(f"{conso_m} kWh — mois {ref_m}") if conso_m else self.style.WARNING("None (aucune donnée)"))
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur requête métier: {e}"))

        self.stdout.write("\n" + "═" * 60 + "\n")

        if not result["tcp_reachable"]:
            self.stdout.write(self.style.WARNING(
                "  → TCP KO : vérifier le tunnel Unifi site-to-site\n"
                "    ping 172.30.0.149 depuis le serveur ?"
            ))
        elif not result["odbc_connected"]:
            self.stdout.write(self.style.WARNING(
                "  → ODBC KO : TCP OK mais login refusé\n"
                "    Vérifier EFMS_SQL_USER / EFMS_SQL_PASSWORD dans .env\n"
                "    Vérifier que le driver 'ODBC Driver 17 for SQL Server' est installé :\n"
                "    docker compose exec web odbcinst -q -d"
            ))
        elif not result["query_ok"]:
            self.stdout.write(self.style.WARNING(
                "  → Query KO : connecté mais requête échoue\n"
                "    Vérifier EFMS_SQL_DB et les droits de l'utilisateur"
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  → Connexion eFMS opérationnelle\n"))

        sys.exit(0 if result["query_ok"] else 1)