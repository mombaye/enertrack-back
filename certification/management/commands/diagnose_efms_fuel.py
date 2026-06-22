import sys
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService


FUEL_TABLES = [
    "[SQL2-ProdDB].[silver].[fact_fuel_order_mth]",
    "[SQL2-ProdDB].[silver].[fact_fuel_deli_mth]",
    "[SQL2-ProdDB].[silver].[fact_fuel_conso_mth]",
    "[SQL2-ProdDB].[silver].[fact_genset_mth]",
]


CANDIDATE_SITE_COLS = [
    "site_id",
    "site",
    "site_name",
    "nom_site",
    "code_site",
    "id_site",
]

CANDIDATE_DATE_COLS = [
    "date",
    "month",
    "mois",
    "period",
    "periode",
    "year_month",
    "date_mois",
    "created_at",
    "updated_at",
]

CANDIDATE_FUEL_COLS = [
    "fuel",
    "carburant",
    "quantity",
    "qty",
    "liter",
    "litre",
    "liters",
    "litres",
    "consumption",
    "conso",
    "delivered",
    "delivery",
    "order",
    "stock",
]


class Command(BaseCommand):
    help = "Diagnostique les tables eFMS Fuel silver.fact_fuel_*"

    def add_arguments(self, parser):
        parser.add_argument(
            "--table",
            type=str,
            default=None,
            help="Nom exact d'une table à diagnostiquer. Si absent, diagnostique toutes les tables fuel connues.",
        )
        parser.add_argument(
            "--site-id",
            type=str,
            default=None,
            help="Filtrer l'échantillon sur un site précis si une colonne site est détectée.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10,
            help="Nombre de lignes échantillon.",
        )

    def _detect_cols(self, columns, candidates):
        out = []
        for col in columns:
            c = col.lower()
            if any(k.lower() in c for k in candidates):
                out.append(col)
        return out

    def _fmt(self, v):
        if v is None:
            return "NULL"
        s = str(v)
        return s[:80] + "..." if len(s) > 80 else s

    def _print_rows(self, rows, columns):
        if not rows:
            self.stdout.write(self.style.WARNING("    Aucune ligne trouvée."))
            return

        selected_cols = columns[:12]
        self.stdout.write("    Colonnes affichées : " + ", ".join(selected_cols))

        for i, row in enumerate(rows, start=1):
            self.stdout.write(f"\n    Ligne #{i}")
            for idx, col in enumerate(selected_cols):
                self.stdout.write(f"      {col:<35}: {self._fmt(row[idx])}")

    def handle(self, *args, **options):
        efms = EfmsService()
        site_id = options.get("site_id")
        limit = options.get("limit") or 10

        tables = [options["table"]] if options.get("table") else FUEL_TABLES

        self.stdout.write("\n" + "═" * 85)
        self.stdout.write("  DIAGNOSTIC — TABLES eFMS FUEL")
        self.stdout.write("═" * 85 + "\n")

        try:
            conn = efms._open_connection()
            self.stdout.write(self.style.SUCCESS("  ✓ Connexion SQL Server OK\n"))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  ✗ Connexion échouée : {e}"))
            sys.exit(1)

        cursor = conn.cursor()

        for table in tables:
            self.stdout.write("\n" + "─" * 85)
            self.stdout.write(self.style.SUCCESS(f"  TABLE : {table}"))
            self.stdout.write("─" * 85)

            try:
                cursor.execute(f"SELECT TOP 0 * FROM {table}")
                columns = [d[0] for d in cursor.description]
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Impossible de lire la structure : {e}"))
                continue

            self.stdout.write(f"\n  Nombre de colonnes : {len(columns)}")

            site_cols = self._detect_cols(columns, CANDIDATE_SITE_COLS)
            date_cols = self._detect_cols(columns, CANDIDATE_DATE_COLS)
            fuel_cols = self._detect_cols(columns, CANDIDATE_FUEL_COLS)

            self.stdout.write("\n  Colonnes site candidates :")
            for c in site_cols or ["Aucune"]:
                self.stdout.write(f"    • {c}")

            self.stdout.write("\n  Colonnes date/période candidates :")
            for c in date_cols or ["Aucune"]:
                self.stdout.write(f"    • {c}")

            self.stdout.write("\n  Colonnes fuel/quantité candidates :")
            for c in fuel_cols or ["Aucune"]:
                self.stdout.write(f"    • {c}")

            self.stdout.write("\n  Toutes les colonnes :")
            for c in columns:
                self.stdout.write(f"    • {c}")

            try:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                total = cursor.fetchone()[0]
                self.stdout.write(self.style.SUCCESS(f"\n  Total lignes : {total}"))
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"\n  Count impossible : {e}"))

            # Date min/max si possible
            if date_cols:
                date_col = date_cols[0]
                try:
                    cursor.execute(f"""
                        SELECT MIN([{date_col}]), MAX([{date_col}])
                        FROM {table}
                        WHERE [{date_col}] IS NOT NULL
                    """)
                    r = cursor.fetchone()
                    self.stdout.write(f"  Période détectée via [{date_col}] : {r[0]} → {r[1]}")
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  Période min/max impossible sur {date_col} : {e}"))

            # Top sites si possible
            if site_cols:
                site_col = site_cols[0]
                try:
                    cursor.execute(f"""
                        SELECT TOP 10 [{site_col}], COUNT(*) AS nb
                        FROM {table}
                        WHERE [{site_col}] IS NOT NULL
                        GROUP BY [{site_col}]
                        ORDER BY nb DESC
                    """)
                    rows = cursor.fetchall()
                    self.stdout.write(f"\n  TOP sites via [{site_col}] :")
                    for r in rows:
                        self.stdout.write(f"    {str(r[0]):<25} {r[1]} lignes")
                except Exception as e:
                    self.stdout.write(self.style.WARNING(f"  TOP sites impossible : {e}"))

            # Agrégats fuel si possible
            self.stdout.write("\n  Agrégats colonnes fuel candidates :")
            for fuel_col in fuel_cols[:8]:
                try:
                    cursor.execute(f"""
                        SELECT
                            COUNT([{fuel_col}]),
                            MIN(TRY_CAST([{fuel_col}] AS FLOAT)),
                            AVG(TRY_CAST([{fuel_col}] AS FLOAT)),
                            MAX(TRY_CAST([{fuel_col}] AS FLOAT)),
                            SUM(TRY_CAST([{fuel_col}] AS FLOAT))
                        FROM {table}
                        WHERE TRY_CAST([{fuel_col}] AS FLOAT) IS NOT NULL
                    """)
                    r = cursor.fetchone()
                    self.stdout.write(
                        f"    {fuel_col:<35} count={r[0]} min={r[1]} avg={r[2]} max={r[3]} sum={r[4]}"
                    )
                except Exception:
                    self.stdout.write(f"    {fuel_col:<35} non numérique ou agrégat impossible")

            # Sample rows
            self.stdout.write(f"\n  Échantillon TOP {limit} :")

            try:
                if site_id and site_cols:
                    site_col = site_cols[0]
                    cursor.execute(f"""
                        SELECT TOP {limit} *
                        FROM {table}
                        WHERE [{site_col}] = ?
                    """, (site_id,))
                else:
                    cursor.execute(f"SELECT TOP {limit} * FROM {table}")

                rows = cursor.fetchall()
                self._print_rows(rows, columns)

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Échantillon impossible : {e}"))

        try:
            conn.close()
        except Exception:
            pass

        self.stdout.write("\n" + "═" * 85)
        self.stdout.write(self.style.SUCCESS("  Diagnostic eFMS Fuel terminé."))
        self.stdout.write("═" * 85 + "\n")