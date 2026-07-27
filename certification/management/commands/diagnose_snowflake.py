# certification/management/commands/diagnose_snowflake.py
"""
Management command pour diagnostiquer la connexion Snowflake et localiser
les tables équivalentes aux tables eFMS SQL Server (silver.gfms_Grid_Report_day,
silver.gfms_AC_Meter_Report_day), en vue de la migration.

Usage:
    docker compose exec web python manage.py diagnose_snowflake
    docker compose exec web python manage.py diagnose_snowflake --find grid
    docker compose exec web python manage.py diagnose_snowflake --table GRID_CONSO_KWH --schema GOLD --columns
    docker compose exec web python manage.py diagnose_snowflake --table GRID_CONSO_KWH --schema GOLD --sample
"""
import sys

from django.core.management.base import BaseCommand

from certification.services.snowflake_service import SnowflakeService


class Command(BaseCommand):
    help = "Diagnostique la connexion Snowflake et cherche les tables équivalentes eFMS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--find", type=str, default=None,
            help="Chercher les tables dont le nom contient ce motif, dans tous les schémas (ex: grid, conso, meter)",
        )
        parser.add_argument(
            "--table", type=str, default=None,
            help="Table à inspecter (avec --columns et/ou --sample)",
        )
        parser.add_argument(
            "--schema", type=str, default=None,
            help="Schéma de la table (défaut: SNOWFLAKE_SCHEMA, ex: GOLD)",
        )
        parser.add_argument(
            "--columns", action="store_true", default=False,
            help="Afficher les colonnes de --table",
        )
        parser.add_argument(
            "--sample", action="store_true", default=False,
            help="Afficher un échantillon de 5 lignes de --table",
        )

    def handle(self, *args, **options):
        sf = SnowflakeService()

        self.stdout.write("\n" + "═" * 60)
        self.stdout.write("  DIAGNOSTIC SNOWFLAKE (DB_GFMS_PROD)")
        self.stdout.write("═" * 60 + "\n")

        result = sf.diagnose()

        def ok(v):
            return self.style.SUCCESS("✓ OK") if v else self.style.ERROR("✗ FAIL")

        self.stdout.write(f"  Compte      : {result['account']}")
        self.stdout.write(f"  Utilisateur : {result['user']}")
        self.stdout.write(f"  Rôle        : {result['role']}")
        self.stdout.write(f"  Warehouse   : {result['warehouse']}")
        self.stdout.write(f"  Base        : {result['database']}")
        self.stdout.write(f"  Schéma      : {result['schema']}")
        self.stdout.write(f"  Connexion   : {ok(result['connected'])}")

        if result["error"]:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(f"  ERREUR : {result['error']}"))
            sys.exit(1)

        if result["schemas"]:
            self.stdout.write(f"\n  Schémas dans {result['database']} :")
            for s in result["schemas"]:
                self.stdout.write(f"    - {s}")

        if result["tables_in_schema"]:
            self.stdout.write(f"\n  Tables dans {result['database']}.{result['schema']} :")
            for t in result["tables_in_schema"]:
                self.stdout.write(f"    - {t}")

        # ── Recherche de tables par motif (tous schémas) ──────────────────────
        find = options.get("find")
        if find:
            self.stdout.write(f"\n  Recherche de tables contenant '{find}' (tous schémas) :")
            try:
                matches = sf.find_tables_like(find)
                if matches:
                    for schema, table in matches:
                        self.stdout.write(f"    {schema}.{table}")
                else:
                    self.stdout.write(self.style.WARNING("    Aucune table trouvée."))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    Erreur recherche : {e}"))

        # ── Colonnes d'une table donnée ────────────────────────────────────────
        table  = options.get("table")
        schema = options.get("schema")
        if table and options.get("columns"):
            self.stdout.write(f"\n  Colonnes de {schema or result['schema']}.{table} :")
            try:
                cols = sf.get_table_columns(table, schema=schema)
                for i, c in enumerate(cols):
                    if any(k in c.lower() for k in ("conso", "energy", "kwh", "power")):
                        self.stdout.write(self.style.SUCCESS(f"    [{i:3d}] {c}  ← énergie/puissance"))
                    elif c.lower() in ("site_id", "date"):
                        self.stdout.write(f"    [{i:3d}] {c}  ← clé")
                    else:
                        self.stdout.write(f"    [{i:3d}] {c}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    Erreur colonnes : {e}"))

        # ── Échantillon ────────────────────────────────────────────────────────
        if table and options.get("sample"):
            self.stdout.write(f"\n  Échantillon de {schema or result['schema']}.{table} :")
            try:
                rows = sf.sample_rows(table, schema=schema, limit=5)
                for r in rows:
                    self.stdout.write(f"    {r}")
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"    Erreur échantillon : {e}"))

        self.stdout.write("\n" + "═" * 60 + "\n")
        sys.exit(0)
