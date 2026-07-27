# fuel_tracking/management/commands/sync_efms_fuel_auto.py

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models import Max

from certification.services.efms import EfmsService
from fuel_tracking.models import FuelEfmsMonthly


TABLES = {
    "ORDER": "[SQL2-ProdDB].[silver].[fact_fuel_order_mth]",
    "DELI": "[SQL2-ProdDB].[silver].[fact_fuel_deli_mth]",
    "CONSO": "[SQL2-ProdDB].[silver].[fact_fuel_conso_mth]",
    "GENSET": "[SQL2-ProdDB].[silver].[fact_genset_mth]",
}


def parse_month(value: str):
    year, month = str(value).split("-")
    return int(year), int(month)


def format_month(year: int, month: int):
    return f"{year:04d}-{month:02d}"


def shift_month(month_year: str, delta: int):
    year, month = parse_month(month_year)
    index = year * 12 + (month - 1) + delta

    new_year = index // 12
    new_month = index % 12 + 1

    return format_month(new_year, new_month)


class Command(BaseCommand):
    help = "Synchronise automatiquement les derniers mois disponibles eFMS Fuel"

    def add_arguments(self, parser):
        parser.add_argument("--country", type=str, default="Senegal")
        parser.add_argument("--rolling-months", type=int, default=4)
        parser.add_argument("--initial-from", type=str, default="2024-01")
        parser.add_argument("--dry-run", action="store_true")

    def _get_latest_months_from_sql(self, country: str):
        efms = EfmsService()
        conn = efms._open_connection()
        cursor = conn.cursor()

        try:
            result = {}

            for key, table in TABLES.items():
                query = f"""
                    SELECT MAX(month_year)
                    FROM {table}
                    WHERE country = ?
                """
                cursor.execute(query, [country])
                row = cursor.fetchone()
                result[key] = row[0] if row and row[0] else None

            return result

        finally:
            try:
                cursor.close()
            except Exception:
                pass

            try:
                conn.close()
            except Exception:
                pass

    def handle(self, *args, **options):
        country = options["country"]
        rolling_months = max(1, int(options["rolling_months"]))
        initial_from = options["initial_from"]
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  AUTO SYNC eFMS FUEL → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Country        : {country}")
        self.stdout.write(f"  Rolling months : {rolling_months}")
        self.stdout.write(f"  Initial from   : {initial_from}")
        self.stdout.write(f"  Dry run        : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        latest_by_table = self._get_latest_months_from_sql(country)

        self.stdout.write("  Derniers mois SQL Server :")
        for key, value in latest_by_table.items():
            self.stdout.write(f"   - {key:<6}: {value or '-'}")

        available_months = [m for m in latest_by_table.values() if m]

        if not available_months:
            self.stdout.write(self.style.WARNING("\n  Aucun mois eFMS disponible côté SQL Server.\n"))
            return

        latest_complete_month = min(available_months)

        self.stdout.write(f"\n  Dernier mois complet retenu : {latest_complete_month}")

        local_latest = (
            FuelEfmsMonthly.objects
            .filter(country__iexact=country)
            .aggregate(v=Max("month_year"))
            ["v"]
        )

        self.stdout.write(f"  Dernier mois local EnerTrack : {local_latest or '-'}")

        if local_latest:
            from_month = shift_month(local_latest, -(rolling_months - 1))

            if from_month < initial_from:
                from_month = initial_from
        else:
            from_month = initial_from

        to_month = latest_complete_month

        if from_month > to_month:
            from_month = shift_month(to_month, -(rolling_months - 1))
            if from_month < initial_from:
                from_month = initial_from

        self.stdout.write("\n  Période à synchroniser :")
        self.stdout.write(f"   - From : {from_month}")
        self.stdout.write(f"   - To   : {to_month}")

        self.stdout.write("\n  Lancement sync_efms_fuel...\n")

        call_command(
            "sync_efms_fuel",
            country=country,
            from_month=from_month,
            to_month=to_month,
            dry_run=dry_run,
        )

        self.stdout.write(self.style.SUCCESS("\n  Auto sync eFMS terminée.\n"))