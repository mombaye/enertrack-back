# fuel_tracking/management/commands/diagnose_efms_fuel.py

from datetime import datetime, date
from decimal import Decimal

from django.core.management.base import BaseCommand

from certification.services.efms import EfmsService
from fuel_tracking.models import FuelEnocMovement


EFMS_TABLES = [
    {
        "key": "ORDER",
        "label": "Commandes carburant",
        "database": "SQL2-ProdDB",
        "schema": "silver",
        "table": "fact_fuel_order_mth",
        "value_column": "fuel_order_l",
        "required_columns": [
            "month_year",
            "country",
            "site_id",
            "site_name",
            "fuel_order_l",
        ],
    },
    {
        "key": "DELI",
        "label": "Livraisons carburant",
        "database": "SQL2-ProdDB",
        "schema": "silver",
        "table": "fact_fuel_deli_mth",
        "value_column": "fuel_deli_l",
        "required_columns": [
            "month_year",
            "country",
            "site_id",
            "site_name",
            "fuel_deli_l",
        ],
    },
    {
        "key": "CONSO",
        "label": "Consommation carburant",
        "database": "SQL2-ProdDB",
        "schema": "silver",
        "table": "fact_fuel_conso_mth",
        "value_column": "fuel_conso_l",
        "required_columns": [
            "month_year",
            "country",
            "site_id",
            "site_name",
            "fuel_conso_l",
        ],
    },
    {
        "key": "GENSET",
        "label": "Heures GE / monitoring",
        "database": "SQL2-ProdDB",
        "schema": "silver",
        "table": "fact_genset_mth",
        "value_column": "ge_working_hours",
        "required_columns": [
            "month_year",
            "country",
            "site_id",
            "ge_working_hours",
            "abnormal_ge_working_hours",
            "monitoring_metering_unavailability_hours",
            "monitoring_metering_unavailability_percent",
        ],
    },
]


def qname(table_def):
    return "[{}].[{}].[{}]".format(
        table_def["database"],
        table_def["schema"],
        table_def["table"],
    )


def clean_value(value):
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def to_float(value):
    if value is None:
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


class Command(BaseCommand):
    help = "Diagnostique les tables eFMS Fuel SQL Server avant synchronisation EnerTrack"

    def add_arguments(self, parser):
        parser.add_argument("--country", type=str, default="Senegal")
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--sample", type=int, default=5)
        parser.add_argument("--compare-enoc", action="store_true")

    def _print_title(self, title):
        self.stdout.write("\n" + "═" * 90)
        self.stdout.write("  {}".format(title))
        self.stdout.write("═" * 90)

    def _print_section(self, title):
        self.stdout.write("\n" + "─" * 90)
        self.stdout.write("  {}".format(title))
        self.stdout.write("─" * 90)

    def _fetch_one(self, cursor, query, params=None):
        cursor.execute(query, params or [])
        return cursor.fetchone()

    def _fetch_all(self, cursor, query, params=None):
        cursor.execute(query, params or [])
        return cursor.fetchall()

    def _columns_for_table(self, cursor, table_def):
        query = """
            SELECT
                COLUMN_NAME,
                DATA_TYPE,
                IS_NULLABLE
            FROM [{}].INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ?
              AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
        """.format(table_def["database"])

        rows = self._fetch_all(
            cursor,
            query,
            [table_def["schema"], table_def["table"]],
        )

        return [
            {
                "name": r[0],
                "type": r[1],
                "nullable": r[2],
            }
            for r in rows
        ]

    def _where_sql(self, month=None, from_month=None, to_month=None, include_country=True):
        filters = []
        params = []

        if include_country:
            filters.append("country = ?")

        if month:
            filters.append("month_year = ?")
        else:
            if from_month:
                filters.append("month_year >= ?")
            if to_month:
                filters.append("month_year <= ?")

        return filters, params

    def _period_filter_sql(self, country, month, from_month, to_month):
        filters = ["country = ?"]
        params = [country]

        if month:
            filters.append("month_year = ?")
            params.append(month)
        else:
            if from_month:
                filters.append("month_year >= ?")
                params.append(from_month)
            if to_month:
                filters.append("month_year <= ?")
                params.append(to_month)

        return " AND ".join(filters), params

    def _diagnose_table_structure(self, cursor, table_def):
        full_table = qname(table_def)

        self._print_section("{} — {}".format(table_def["key"], table_def["label"]))
        self.stdout.write("  Table : {}".format(full_table))

        columns = self._columns_for_table(cursor, table_def)
        column_names = {c["name"] for c in columns}

        if not columns:
            self.stdout.write(self.style.ERROR("  ❌ Table introuvable ou aucune colonne visible."))
            return {
                "exists": False,
                "missing_columns": table_def["required_columns"],
                "columns": [],
            }

        self.stdout.write(self.style.SUCCESS("  ✅ Table accessible"))
        self.stdout.write("  Colonnes : {}".format(len(columns)))

        missing = [c for c in table_def["required_columns"] if c not in column_names]

        if missing:
            self.stdout.write(self.style.ERROR("  ❌ Colonnes manquantes : {}".format(", ".join(missing))))
        else:
            self.stdout.write(self.style.SUCCESS("  ✅ Colonnes obligatoires OK"))

        self.stdout.write("\n  Colonnes détectées :")
        for c in columns:
            self.stdout.write(
                "   - {} ({}, nullable={})".format(
                    c["name"],
                    c["type"],
                    c["nullable"],
                )
            )

        return {
            "exists": True,
            "missing_columns": missing,
            "columns": columns,
        }

    def _diagnose_table_volume(self, cursor, table_def, country, month, from_month, to_month):
        full_table = qname(table_def)
        value_column = table_def["value_column"]

        where_sql, params = self._period_filter_sql(country, month, from_month, to_month)

        query_global = """
            SELECT
                COUNT_BIG(*) AS rows_count,
                COUNT(DISTINCT month_year) AS months_count,
                MIN(month_year) AS min_month,
                MAX(month_year) AS max_month,
                COUNT(DISTINCT country) AS countries_count,
                COUNT(DISTINCT site_id) AS sites_count
            FROM {}
        """.format(full_table)

        row = self._fetch_one(cursor, query_global)

        self.stdout.write("\n  Volume global table :")
        self.stdout.write("   - Lignes totales       : {}".format(clean_value(row[0])))
        self.stdout.write("   - Mois distincts       : {}".format(clean_value(row[1])))
        self.stdout.write("   - Période globale      : {} → {}".format(clean_value(row[2]), clean_value(row[3])))
        self.stdout.write("   - Pays distincts       : {}".format(clean_value(row[4])))
        self.stdout.write("   - Sites distincts      : {}".format(clean_value(row[5])))

        query_scope = """
            SELECT
                COUNT_BIG(*) AS rows_count,
                COUNT(DISTINCT month_year) AS months_count,
                MIN(month_year) AS min_month,
                MAX(month_year) AS max_month,
                COUNT(DISTINCT site_id) AS sites_count,
                SUM(TRY_CAST({value_column} AS FLOAT)) AS value_sum,
                AVG(TRY_CAST({value_column} AS FLOAT)) AS value_avg,
                MIN(TRY_CAST({value_column} AS FLOAT)) AS value_min,
                MAX(TRY_CAST({value_column} AS FLOAT)) AS value_max
            FROM {table}
            WHERE {where_sql}
        """.format(
            table=full_table,
            value_column=value_column,
            where_sql=where_sql,
        )

        scoped = self._fetch_one(cursor, query_scope, params)

        self.stdout.write("\n  Volume périmètre demandé :")
        self.stdout.write("   - Country              : {}".format(country))
        self.stdout.write("   - Filtre mois          : {}".format(month or "{} → {}".format(from_month or "-", to_month or "-")))
        self.stdout.write("   - Lignes               : {}".format(clean_value(scoped[0])))
        self.stdout.write("   - Mois distincts       : {}".format(clean_value(scoped[1])))
        self.stdout.write("   - Période trouvée      : {} → {}".format(clean_value(scoped[2]), clean_value(scoped[3])))
        self.stdout.write("   - Sites distincts      : {}".format(clean_value(scoped[4])))
        self.stdout.write("   - Somme {}      : {}".format(value_column, clean_value(scoped[5])))
        self.stdout.write("   - Moyenne {}    : {}".format(value_column, clean_value(scoped[6])))
        self.stdout.write("   - Min / Max            : {} / {}".format(clean_value(scoped[7]), clean_value(scoped[8])))

        query_quality = """
            SELECT
                SUM(CASE WHEN country IS NULL OR LTRIM(RTRIM(CAST(country AS NVARCHAR(255)))) = '' THEN 1 ELSE 0 END) AS missing_country,
                SUM(CASE WHEN month_year IS NULL OR LTRIM(RTRIM(CAST(month_year AS NVARCHAR(255)))) = '' THEN 1 ELSE 0 END) AS missing_month,
                SUM(CASE WHEN site_id IS NULL OR LTRIM(RTRIM(CAST(site_id AS NVARCHAR(255)))) = '' THEN 1 ELSE 0 END) AS missing_site,
                SUM(CASE WHEN {value_column} IS NOT NULL AND TRY_CAST({value_column} AS FLOAT) IS NULL THEN 1 ELSE 0 END) AS invalid_value,
                SUM(CASE WHEN TRY_CAST({value_column} AS FLOAT) < 0 THEN 1 ELSE 0 END) AS negative_value,
                SUM(CASE WHEN TRY_CAST({value_column} AS FLOAT) = 0 THEN 1 ELSE 0 END) AS zero_value
            FROM {table}
            WHERE {where_sql}
        """.format(
            table=full_table,
            value_column=value_column,
            where_sql=where_sql,
        )

        quality = self._fetch_one(cursor, query_quality, params)

        self.stdout.write("\n  Qualité des données :")
        self.stdout.write("   - country manquant     : {}".format(clean_value(quality[0])))
        self.stdout.write("   - month_year manquant  : {}".format(clean_value(quality[1])))
        self.stdout.write("   - site_id manquant     : {}".format(clean_value(quality[2])))
        self.stdout.write("   - valeur invalide      : {}".format(clean_value(quality[3])))
        self.stdout.write("   - valeur négative      : {}".format(clean_value(quality[4])))
        self.stdout.write("   - valeur zéro          : {}".format(clean_value(quality[5])))

        return {
            "rows": int(scoped[0] or 0),
            "sites": int(scoped[4] or 0),
            "sum": to_float(scoped[5]),
            "min_month": scoped[2],
            "max_month": scoped[3],
            "missing_site": int(quality[2] or 0),
            "invalid_value": int(quality[3] or 0),
            "negative_value": int(quality[4] or 0),
        }

    def _print_country_breakdown(self, cursor, table_def):
        full_table = qname(table_def)

        query = """
            SELECT TOP 10
                country,
                COUNT_BIG(*) AS rows_count,
                COUNT(DISTINCT site_id) AS sites_count,
                MIN(month_year) AS min_month,
                MAX(month_year) AS max_month
            FROM {}
            GROUP BY country
            ORDER BY rows_count DESC
        """.format(full_table)

        rows = self._fetch_all(cursor, query)

        self.stdout.write("\n  Top pays :")
        for r in rows:
            self.stdout.write(
                "   - {} | lignes={} | sites={} | période={}→{}".format(
                    clean_value(r[0]),
                    clean_value(r[1]),
                    clean_value(r[2]),
                    clean_value(r[3]),
                    clean_value(r[4]),
                )
            )

    def _print_monthly_breakdown(self, cursor, table_def, country, month, from_month, to_month):
        full_table = qname(table_def)
        value_column = table_def["value_column"]
        where_sql, params = self._period_filter_sql(country, month, from_month, to_month)

        query = """
            SELECT
                month_year,
                COUNT_BIG(*) AS rows_count,
                COUNT(DISTINCT site_id) AS sites_count,
                SUM(TRY_CAST({value_column} AS FLOAT)) AS value_sum
            FROM {table}
            WHERE {where_sql}
            GROUP BY month_year
            ORDER BY month_year DESC
        """.format(
            table=full_table,
            value_column=value_column,
            where_sql=where_sql,
        )

        rows = self._fetch_all(cursor, query, params)

        self.stdout.write("\n  Breakdown mensuel :")
        for r in rows[:18]:
            self.stdout.write(
                "   - {} | lignes={} | sites={} | somme {}={}".format(
                    clean_value(r[0]),
                    clean_value(r[1]),
                    clean_value(r[2]),
                    value_column,
                    clean_value(r[3]),
                )
            )

    def _print_sample(self, cursor, table_def, country, month, from_month, to_month, sample):
        full_table = qname(table_def)
        value_column = table_def["value_column"]
        where_sql, params = self._period_filter_sql(country, month, from_month, to_month)

        query = """
            SELECT TOP {}
                month_year,
                country,
                site_id,
                {}
            FROM {}
            WHERE {}
            ORDER BY month_year DESC, site_id
        """.format(sample, value_column, full_table, where_sql)

        rows = self._fetch_all(cursor, query, params)

        self.stdout.write("\n  Échantillon :")
        if not rows:
            self.stdout.write("   - Aucun exemple trouvé.")
            return

        for r in rows:
            self.stdout.write(
                "   - month={} | country={} | site={} | {}={}".format(
                    clean_value(r[0]),
                    clean_value(r[1]),
                    clean_value(r[2]),
                    value_column,
                    clean_value(r[3]),
                )
            )

    def _diagnose_overlap(self, cursor, country, month, from_month, to_month):
        self._print_section("Recoupement des sites entre tables eFMS")

        where_sql, params = self._period_filter_sql(country, month, from_month, to_month)

        query = """
            WITH
            order_sites AS (
                SELECT DISTINCT site_id
                FROM {order_table}
                WHERE {where_sql}
                  AND site_id IS NOT NULL
            ),
            deli_sites AS (
                SELECT DISTINCT site_id
                FROM {deli_table}
                WHERE {where_sql}
                  AND site_id IS NOT NULL
            ),
            conso_sites AS (
                SELECT DISTINCT site_id
                FROM {conso_table}
                WHERE {where_sql}
                  AND site_id IS NOT NULL
            ),
            genset_sites AS (
                SELECT DISTINCT site_id
                FROM {genset_table}
                WHERE {where_sql}
                  AND site_id IS NOT NULL
            ),
            union_sites AS (
                SELECT site_id FROM order_sites
                UNION
                SELECT site_id FROM deli_sites
                UNION
                SELECT site_id FROM conso_sites
                UNION
                SELECT site_id FROM genset_sites
            )
            SELECT
                (SELECT COUNT(*) FROM union_sites) AS union_sites,
                (SELECT COUNT(*) FROM order_sites) AS order_sites,
                (SELECT COUNT(*) FROM deli_sites) AS deli_sites,
                (SELECT COUNT(*) FROM conso_sites) AS conso_sites,
                (SELECT COUNT(*) FROM genset_sites) AS genset_sites,
                (
                    SELECT COUNT(*)
                    FROM union_sites u
                    WHERE EXISTS (SELECT 1 FROM order_sites o WHERE o.site_id = u.site_id)
                      AND EXISTS (SELECT 1 FROM deli_sites d WHERE d.site_id = u.site_id)
                      AND EXISTS (SELECT 1 FROM conso_sites c WHERE c.site_id = u.site_id)
                      AND EXISTS (SELECT 1 FROM genset_sites g WHERE g.site_id = u.site_id)
                ) AS sites_in_all_tables,
                (
                    SELECT COUNT(*)
                    FROM conso_sites c
                    WHERE NOT EXISTS (SELECT 1 FROM genset_sites g WHERE g.site_id = c.site_id)
                ) AS conso_without_genset,
                (
                    SELECT COUNT(*)
                    FROM genset_sites g
                    WHERE NOT EXISTS (SELECT 1 FROM conso_sites c WHERE c.site_id = g.site_id)
                ) AS genset_without_conso
        """.format(
            order_table=qname(EFMS_TABLES[0]),
            deli_table=qname(EFMS_TABLES[1]),
            conso_table=qname(EFMS_TABLES[2]),
            genset_table=qname(EFMS_TABLES[3]),
            where_sql=where_sql,
        )

        # Le même filtre est répété 4 fois dans les CTE.
        row = self._fetch_one(cursor, query, params * 4)

        self.stdout.write("  Périmètre : country={} | mois={}".format(
            country,
            month or "{}→{}".format(from_month or "-", to_month or "-"),
        ))
        self.stdout.write("  Sites union eFMS             : {}".format(clean_value(row[0])))
        self.stdout.write("  Sites avec order             : {}".format(clean_value(row[1])))
        self.stdout.write("  Sites avec delivery          : {}".format(clean_value(row[2])))
        self.stdout.write("  Sites avec conso             : {}".format(clean_value(row[3])))
        self.stdout.write("  Sites avec genset            : {}".format(clean_value(row[4])))
        self.stdout.write("  Sites présents dans les 4    : {}".format(clean_value(row[5])))
        self.stdout.write("  Conso sans genset            : {}".format(clean_value(row[6])))
        self.stdout.write("  Genset sans conso            : {}".format(clean_value(row[7])))

    def _compare_with_enoc(self, cursor, country, month, from_month, to_month):
        self._print_section("Comparaison couverture eFMS vs ENOC local")

        if not month:
            self.stdout.write("  Comparaison ENOC disponible uniquement avec --month YYYY-MM.")
            return

        year, month_num = [int(x) for x in month.split("-")]

        where_sql, params = self._period_filter_sql(country, month, None, None)

        query = """
            WITH all_efms_sites AS (
                SELECT DISTINCT site_id FROM {order_table} WHERE {where_sql}
                UNION
                SELECT DISTINCT site_id FROM {deli_table} WHERE {where_sql}
                UNION
                SELECT DISTINCT site_id FROM {conso_table} WHERE {where_sql}
                UNION
                SELECT DISTINCT site_id FROM {genset_table} WHERE {where_sql}
            )
            SELECT site_id
            FROM all_efms_sites
            WHERE site_id IS NOT NULL
        """.format(
            order_table=qname(EFMS_TABLES[0]),
            deli_table=qname(EFMS_TABLES[1]),
            conso_table=qname(EFMS_TABLES[2]),
            genset_table=qname(EFMS_TABLES[3]),
            where_sql=where_sql,
        )

        rows = self._fetch_all(cursor, query, params * 4)
        efms_sites = {str(r[0]).strip() for r in rows if r[0]}

        enoc_sites = set(
            FuelEnocMovement.objects.filter(
                operation_date__year=year,
                operation_date__month=month_num,
            )
            .exclude(site_id__isnull=True)
            .values_list("site_id", flat=True)
        )

        common = efms_sites & enoc_sites
        enoc_only = enoc_sites - efms_sites
        efms_only = efms_sites - enoc_sites

        self.stdout.write("  Mois                         : {}".format(month))
        self.stdout.write("  Sites eFMS                   : {}".format(len(efms_sites)))
        self.stdout.write("  Sites ENOC importés          : {}".format(len(enoc_sites)))
        self.stdout.write("  Sites communs eFMS + ENOC    : {}".format(len(common)))
        self.stdout.write("  Sites ENOC absents eFMS      : {}".format(len(enoc_only)))
        self.stdout.write("  Sites eFMS absents ENOC      : {}".format(len(efms_only)))

        if enoc_only:
            self.stdout.write("\n  Exemples ENOC absents eFMS :")
            for site in sorted(list(enoc_only))[:10]:
                self.stdout.write("   - {}".format(site))

        if efms_only:
            self.stdout.write("\n  Exemples eFMS absents ENOC :")
            for site in sorted(list(efms_only))[:10]:
                self.stdout.write("   - {}".format(site))

    def handle(self, *args, **options):
        country = options["country"]
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        sample = options["sample"]
        compare_enoc = options["compare_enoc"]

        if month:
            from_month = month
            to_month = month

        self._print_title("DIAGNOSTIC eFMS FUEL → EnerTrack")
        self.stdout.write("  Country       : {}".format(country))
        self.stdout.write("  Month         : {}".format(month or "-"))
        self.stdout.write("  From month    : {}".format(from_month or "-"))
        self.stdout.write("  To month      : {}".format(to_month or "-"))
        self.stdout.write("  Sample        : {}".format(sample))
        self.stdout.write("  Compare ENOC  : {}".format(compare_enoc))

        efms = EfmsService()
        conn = efms._open_connection()
        cursor = conn.cursor()

        results = {}

        try:
            for table_def in EFMS_TABLES:
                structure = self._diagnose_table_structure(cursor, table_def)

                if not structure["exists"]:
                    results[table_def["key"]] = {
                        "exists": False,
                        "rows": 0,
                        "sites": 0,
                        "sum": 0,
                        "missing_columns": structure["missing_columns"],
                    }
                    continue

                self._print_country_breakdown(cursor, table_def)

                volume = self._diagnose_table_volume(
                    cursor=cursor,
                    table_def=table_def,
                    country=country,
                    month=month,
                    from_month=from_month,
                    to_month=to_month,
                )

                self._print_monthly_breakdown(
                    cursor=cursor,
                    table_def=table_def,
                    country=country,
                    month=month,
                    from_month=from_month,
                    to_month=to_month,
                )

                self._print_sample(
                    cursor=cursor,
                    table_def=table_def,
                    country=country,
                    month=month,
                    from_month=from_month,
                    to_month=to_month,
                    sample=sample,
                )

                results[table_def["key"]] = {
                    "exists": True,
                    "rows": volume["rows"],
                    "sites": volume["sites"],
                    "sum": volume["sum"],
                    "missing_columns": structure["missing_columns"],
                    "missing_site": volume["missing_site"],
                    "invalid_value": volume["invalid_value"],
                    "negative_value": volume["negative_value"],
                }

            self._diagnose_overlap(cursor, country, month, from_month, to_month)

            if compare_enoc:
                self._compare_with_enoc(cursor, country, month, from_month, to_month)

            self._print_title("SYNTHÈSE DIAGNOSTIC eFMS FUEL")

            has_blocking_issue = False

            for key, info in results.items():
                if not info["exists"]:
                    status = "BLOQUANT"
                    has_blocking_issue = True
                elif info["missing_columns"]:
                    status = "BLOQUANT"
                    has_blocking_issue = True
                elif info["rows"] == 0:
                    status = "À VÉRIFIER"
                elif info["invalid_value"] > 0 or info["negative_value"] > 0:
                    status = "À CONTRÔLER"
                else:
                    status = "OK"

                self.stdout.write(
                    "  {} | statut={} | lignes={} | sites={} | somme={} | missing_cols={} | invalid={} | negative={}".format(
                        key,
                        status,
                        info["rows"],
                        info["sites"],
                        round(info["sum"], 3),
                        ",".join(info["missing_columns"]) if info["missing_columns"] else "-",
                        info["invalid_value"],
                        info["negative_value"],
                    )
                )

            if has_blocking_issue:
                self.stdout.write(self.style.ERROR("\n  ❌ Diagnostic terminé avec point bloquant.\n"))
            else:
                self.stdout.write(self.style.SUCCESS("\n  ✅ Diagnostic terminé. Pas de point bloquant détecté.\n"))

        finally:
            try:
                cursor.close()
            except Exception:
                pass

            try:
                conn.close()
            except Exception:
                pass