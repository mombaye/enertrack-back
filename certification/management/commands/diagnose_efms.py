# certification/management/commands/diagnose_efms.py
"""
Management command pour diagnostiquer la connexion eFMS.
Usage:
    docker compose exec web python manage.py diagnose_efms
    docker compose exec web python manage.py diagnose_efms --site-id DKR_0028
    docker compose exec web python manage.py diagnose_efms --table gfms_Grid_Report_day_test
    docker compose exec web python manage.py diagnose_efms --table gfms_Grid_Report_day_test --site-id DKR_0028
    docker compose exec web python manage.py diagnose_efms --list-columns --table gfms_Grid_Report_day_test
"""
import sys

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService


class Command(BaseCommand):
    help = "Diagnostique la connexion SQL Server eFMS"

    def add_arguments(self, parser):
        parser.add_argument(
            "--site-id", type=str, default=None,
            help="Tester une requête métier sur ce site_id",
        )
        parser.add_argument(
            "--table", type=str, default=None,
            help=(
                "Overrider la table Grid à tester "
                "(ex: gfms_Grid_Report_day_test). "
                "Si absent, utilise la table configurée dans le code."
            ),
        )
        parser.add_argument(
            "--list-columns", action="store_true", default=False,
            help="Afficher toutes les colonnes de la table Grid (utile pour vérifier kVAh, kVARh...)",
        )
        parser.add_argument(
            "--count-sites", action="store_true", default=False,
            help="Compter le nombre de site_ids distincts dans la table Grid",
        )

    def handle(self, *args, **options):
        table_override = options.get("table")
        site_id        = options.get("site_id")
        list_columns   = options.get("list_columns")
        count_sites    = options.get("count_sites")

        self.stdout.write("\n" + "═" * 60)
        self.stdout.write("  DIAGNOSTIC eFMS SQL SERVER")
        self.stdout.write("═" * 60 + "\n")

        efms = EfmsService()

        # ── Override table si demandé ─────────────────────────────────────────
        original_table = EfmsService.TABLE_GRID
        if table_override:
            table_name = f"[SQL1-ProdDB].[dbo].[silver.{table_override}]"
            EfmsService.TABLE_GRID = table_name
            # Reset la résolution de colonne car la nouvelle table peut avoir des colonnes différentes
            EfmsService._col_conso_resolved = None
            self.stdout.write(self.style.WARNING(f"  ⚠ TABLE GRID OVERRIDÉE → {table_name}"))
            self.stdout.write("")

        result = efms.diagnose()

        def ok(v):
            return self.style.SUCCESS("✓ OK") if v else self.style.ERROR("✗ FAIL")

        self.stdout.write(f"  Table Grid     : {EfmsService.TABLE_GRID}")
        self.stdout.write(f"  Hôte           : {result['host']}:{result['port']}")
        self.stdout.write(f"  Base           : {result['db']}")
        self.stdout.write(f"  Utilisateur    : {result['user']}")
        self.stdout.write(f"  Driver         : {result['driver']}")
        self.stdout.write("")
        self.stdout.write(f"  TCP (port 1433)      : {ok(result['tcp_reachable'])}")
        self.stdout.write(f"  Connexion ODBC       : {ok(result['odbc_connected'])}")
        self.stdout.write(f"  Requête SELECT 1     : {ok(result['query_ok'])}")

        if result["col_conso_resolved"]:
            self.stdout.write(f"  Colonne conso résolue: {self.style.SUCCESS(result['col_conso_resolved'])}")

        if result["row_count_test"] is not None:
            self.stdout.write(f"  Table Grid (TOP 5)   : {self.style.SUCCESS(str(result['row_count_test'])) } lignes")

        if result.get("mode_guess"):
            self.stdout.write(f"  Mode détecté         : {result['mode_guess']} (delta=index, sum=daily)")

        if result.get("sample_5_rows"):
            self.stdout.write("\n  Échantillon données :")
            for r in result["sample_5_rows"]:
                self.stdout.write(f"    site_id={r['site_id']}  date={r['date']}  conso={r['conso']:.3f}")

        if result["error"]:
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(f"  ERREUR : {result['error']}"))

        # ── Afficher toutes les colonnes ──────────────────────────────────────
        if list_columns and result["table_columns"]:
            self.stdout.write("\n  Colonnes de la table :")
            for i, col in enumerate(result["table_columns"]):
                # Mettre en évidence les colonnes importantes
                if any(k in col.lower() for k in ("kvah", "kvarh", "energy", "conso", "kwh", "pav", "power")):
                    self.stdout.write(self.style.SUCCESS(f"    [{i:3d}] {col}  ← énergie/puissance"))
                elif col.lower() in ("site_id", "date"):
                    self.stdout.write(f"    [{i:3d}] {col}  ← clé")
                else:
                    self.stdout.write(f"    [{i:3d}] {col}")

        # ── Compter les site_ids distincts ────────────────────────────────────
        if count_sites and result["query_ok"]:
            self.stdout.write("\n  Comptage des site_ids distincts...")
            try:
                import pyodbc
                conn = pyodbc.connect(efms._build_conn_string(), timeout=efms.timeout)
                cursor = conn.cursor()
                cursor.execute(f"SELECT COUNT(DISTINCT site_id) FROM {EfmsService.TABLE_GRID}")
                nb = cursor.fetchone()[0]
                conn.close()
                self.stdout.write(self.style.SUCCESS(f"  site_ids distincts : {nb}"))
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur comptage : {e}"))

        # ── Test requête métier ───────────────────────────────────────────────
        if site_id and result["query_ok"]:
            self.stdout.write("")
            self.stdout.write(f"  Test métier pour site_id={site_id!r} ...")
            from datetime import date, timedelta
            end   = date.today()
            start = end - timedelta(days=30)
            try:
                conso, method = efms.get_conso_periode(site_id, start, end)
                self.stdout.write(
                    f"  get_conso_periode({start} → {end}) : "
                    + (self.style.SUCCESS(f"{conso} kWh [{method}]")
                       if conso else self.style.WARNING("None (aucune donnée)"))
                )
                conso_m, ref_m = efms.get_conso_last_complete_month(site_id, end)
                self.stdout.write(
                    f"  get_conso_last_complete_month     : "
                    + (self.style.SUCCESS(f"{conso_m} kWh — mois {ref_m}")
                       if conso_m else self.style.WARNING("None (aucune donnée)"))
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur requête métier: {e}"))

        # ── Restaurer la table originale ──────────────────────────────────────
        if table_override:
            EfmsService.TABLE_GRID = original_table
            EfmsService._col_conso_resolved = None

        # ── Résumé et conseils ────────────────────────────────────────────────
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
                "    Vérifier le driver : docker compose exec web odbcinst -q -d"
            ))
        elif not result["query_ok"]:
            self.stdout.write(self.style.WARNING(
                "  → Query KO : connecté mais requête échoue\n"
                "    Vérifier EFMS_SQL_DB et les droits de l'utilisateur"
            ))
        else:
            self.stdout.write(self.style.SUCCESS("  → Connexion eFMS opérationnelle\n"))

        sys.exit(0 if result["query_ok"] else 1)