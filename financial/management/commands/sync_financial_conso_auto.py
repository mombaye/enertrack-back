# financial/management/commands/sync_financial_conso_auto.py
"""Synchronise automatiquement les derniers mois disponibles (fenêtre glissante)."""
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.models import Max

from financial.models import FinancialConsoMonthly


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
    help = "Synchronise automatiquement les derniers mois disponibles de conso financière"

    def add_arguments(self, parser):
        parser.add_argument("--rolling-months", type=int, default=4)
        parser.add_argument("--initial-from", type=str, default="2024-01")
        parser.add_argument("--dry-run", action="store_true")

    def _latest_month_snowflake_grid(self) -> str | None:
        from certification.services.snowflake_service import SnowflakeService

        sf = SnowflakeService()
        conn = sf._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT MAX(DATE) FROM {sf.database}.{sf.schema}.GRID_REPORT")
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            d = row[0]
            return format_month(d.year, d.month)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _latest_month_sql2_solar(self) -> str | None:
        import pyodbc

        host     = getattr(settings, "EFMS_SQL_HOST", "172.30.0.149")
        port     = int(getattr(settings, "EFMS_SQL_PORT", 1433))
        user     = getattr(settings, "EFMS_SQL_USER", "")
        password = getattr(settings, "EFMS_SQL_PASSWORD", "")
        driver   = getattr(settings, "EFMS_SQL_DRIVER", "ODBC Driver 17 for SQL Server")

        conn_str = (
            f"DRIVER={{{driver}}};SERVER={host},{port};DATABASE=SQL2-ProdDB;"
            f"UID={user};PWD={password};TrustServerCertificate=yes;Connection Timeout=8;"
        )
        conn = pyodbc.connect(conn_str, timeout=8)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(month_year) FROM [SQL2-ProdDB].[silver].[fact_solar_mth]")
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            my = row[0]
            if hasattr(my, "month"):
                return format_month(my.year, my.month)
            return str(my)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def handle(self, *args, **options):
        rolling_months = max(1, int(options["rolling_months"]))
        initial_from = options["initial_from"]
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  AUTO SYNC CONSO FINANCIER → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Rolling months : {rolling_months}")
        self.stdout.write(f"  Initial from   : {initial_from}")
        self.stdout.write(f"  Dry run        : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        grid_month = None
        try:
            grid_month = self._latest_month_snowflake_grid()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Erreur lecture Snowflake GRID_REPORT : {e}"))

        solar_month = None
        try:
            solar_month = self._latest_month_sql2_solar()
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Erreur lecture SQL Server fact_solar_mth : {e}"))

        if not grid_month and not solar_month:
            self.stdout.write(self.style.WARNING("\n  Aucun mois disponible côté source.\n"))
            return

        # ✅ Le Grid/ACM (Snowflake, quotidien) pilote la fenêtre à synchroniser
        # — on ne bloque plus sur le Solaire (SQL Server fact_solar_mth, qui a
        # son propre retard de publication, souvent 1-2 mois derrière). Avant
        # ce fix, un `min()` des deux sources faisait plafonner tout le sync
        # (Grid ET ACM inclus) au mois du composant le plus en retard, alors
        # que Grid/ACM étaient déjà à jour au jour le jour. Le Solaire reste
        # simplement à 0 pour les mois où sa source n'a pas encore publié,
        # et se remplira tout seul au prochain run une fois disponible
        # (sync_financial_conso fait un upsert, donc un mois déjà synchronisé
        # se complète automatiquement sans double-compte).
        latest_complete_month = grid_month or solar_month
        if solar_month and solar_month < latest_complete_month:
            self.stdout.write(
                self.style.WARNING(
                    f"  Solaire (SQL Server) en retard : dernier mois publié {solar_month}, "
                    f"Grid/ACM (Snowflake) déjà à {latest_complete_month} — synchronisé quand même, "
                    "solaire à 0 en attendant que la source rattrape."
                )
            )
        self.stdout.write(f"  Dernier mois retenu (piloté par Grid/ACM) : {latest_complete_month}")

        local_latest = FinancialConsoMonthly.objects.aggregate(v=Max("year"))["v"]
        if local_latest:
            local_latest_row = (
                FinancialConsoMonthly.objects.filter(year=local_latest).aggregate(v=Max("month"))
            )
            local_latest_month = format_month(local_latest, local_latest_row["v"])
        else:
            local_latest_month = None

        self.stdout.write(f"  Dernier mois local EnerTrack : {local_latest_month or '-'}")

        if local_latest_month:
            from_month = shift_month(local_latest_month, -(rolling_months - 1))
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

        self.stdout.write("\n  Lancement sync_financial_conso...\n")

        call_command(
            "sync_financial_conso",
            from_month=from_month,
            to_month=to_month,
            dry_run=dry_run,
        )

        self.stdout.write(self.style.SUCCESS("\n  Auto sync conso financier terminée.\n"))
