from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from certification.services.efms import EfmsService
from fuel_tracking.models import FuelEfmsMonthly, FuelEfmsSyncRun


TABLE_ORDER = "[SQL2-ProdDB].[silver].[fact_fuel_order_mth]"
TABLE_DELI = "[SQL2-ProdDB].[silver].[fact_fuel_deli_mth]"
TABLE_CONSO = "[SQL2-ProdDB].[silver].[fact_fuel_conso_mth]"
TABLE_GENSET = "[SQL2-ProdDB].[silver].[fact_genset_mth]"


def to_decimal(value, default="0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def parse_month_year(month_year: str) -> tuple[int, int]:
    raw = str(month_year or "").strip()
    year, month = raw.split("-")
    return int(year), int(month)


def build_anomalies(row: dict) -> list[str]:
    anomalies = []

    fuel_order = row["fuel_order_l"]
    fuel_deli = row["fuel_deli_l"]
    fuel_conso = row["fuel_conso_l"]
    ge_hours = row["ge_working_hours"]

    if fuel_deli > fuel_order and fuel_order > 0:
        anomalies.append("DELIVERY_GT_ORDER")

    if fuel_conso > 0 and ge_hours == 0:
        anomalies.append("CONSO_WITHOUT_GE_HOURS")

    if ge_hours > 0 and fuel_conso == 0:
        anomalies.append("GE_HOURS_WITHOUT_CONSO")

    if row["abnormal_ge_working_hours"] > 0:
        anomalies.append("ABNORMAL_GE_HOURS")

    if row["monitoring_unavailability_percent"] >= Decimal("20"):
        anomalies.append("HIGH_MONITORING_UNAVAILABILITY")

    return anomalies


class Command(BaseCommand):
    help = "Synchronise les données eFMS Fuel mensuelles vers EnerTrack"

    def add_arguments(self, parser):
        parser.add_argument("--country", type=str, default="Senegal")
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--dry-run", action="store_true")

    def _where_sql(self, country, month, from_month, to_month):
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

    def handle(self, *args, **options):
        country = options["country"]
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        dry_run = options["dry_run"]

        if month:
            from_month = month
            to_month = month

        sync_run = FuelEfmsSyncRun.objects.create(
            country=country,
            month_from=from_month,
            month_to=to_month,
            status=FuelEfmsSyncRun.Status.RUNNING,
        )

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC eFMS FUEL → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Country    : {country}")
        self.stdout.write(f"  From month : {from_month or '-'}")
        self.stdout.write(f"  To month   : {to_month or '-'}")
        self.stdout.write(f"  Dry run    : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        try:
            where_sql, base_params = self._where_sql(country, month, from_month, to_month)

            query = f"""
                WITH
                order_mth AS (
                    SELECT
                        month_year,
                        country,
                        site_id,
                        MAX(site_name) AS site_name,
                        SUM(TRY_CAST(fuel_order_l AS FLOAT)) AS fuel_order_l
                    FROM {TABLE_ORDER}
                    WHERE {where_sql}
                    GROUP BY month_year, country, site_id
                ),
                deli_mth AS (
                    SELECT
                        month_year,
                        country,
                        site_id,
                        MAX(site_name) AS site_name,
                        SUM(TRY_CAST(fuel_deli_l AS FLOAT)) AS fuel_deli_l
                    FROM {TABLE_DELI}
                    WHERE {where_sql}
                    GROUP BY month_year, country, site_id
                ),
                conso_mth AS (
                    SELECT
                        month_year,
                        country,
                        site_id,
                        MAX(site_name) AS site_name,
                        SUM(TRY_CAST(fuel_conso_l AS FLOAT)) AS fuel_conso_l
                    FROM {TABLE_CONSO}
                    WHERE {where_sql}
                    GROUP BY month_year, country, site_id
                ),
                genset_mth AS (
                    SELECT
                        month_year,
                        country,
                        site_id,
                        SUM(TRY_CAST(ge_working_hours AS FLOAT)) AS ge_working_hours,
                        SUM(TRY_CAST(abnormal_ge_working_hours AS FLOAT)) AS abnormal_ge_working_hours,
                        SUM(TRY_CAST(monitoring_metering_unavailability_hours AS FLOAT)) AS monitoring_unavailability_hours,
                        AVG(TRY_CAST(monitoring_metering_unavailability_percent AS FLOAT)) AS monitoring_unavailability_percent
                    FROM {TABLE_GENSET}
                    WHERE {where_sql}
                    GROUP BY month_year, country, site_id
                ),
                keys_union AS (
                    SELECT month_year, country, site_id FROM order_mth
                    UNION
                    SELECT month_year, country, site_id FROM deli_mth
                    UNION
                    SELECT month_year, country, site_id FROM conso_mth
                    UNION
                    SELECT month_year, country, site_id FROM genset_mth
                )
                SELECT
                    k.month_year,
                    k.country,
                    k.site_id,
                    COALESCE(o.site_name, d.site_name, c.site_name) AS site_name,

                    COALESCE(o.fuel_order_l, 0) AS fuel_order_l,
                    COALESCE(d.fuel_deli_l, 0) AS fuel_deli_l,
                    COALESCE(c.fuel_conso_l, 0) AS fuel_conso_l,

                    COALESCE(g.ge_working_hours, 0) AS ge_working_hours,
                    COALESCE(g.abnormal_ge_working_hours, 0) AS abnormal_ge_working_hours,
                    COALESCE(g.monitoring_unavailability_hours, 0) AS monitoring_unavailability_hours,
                    COALESCE(g.monitoring_unavailability_percent, 0) AS monitoring_unavailability_percent
                FROM keys_union k
                LEFT JOIN order_mth o
                    ON o.month_year = k.month_year
                    AND o.country = k.country
                    AND o.site_id = k.site_id
                LEFT JOIN deli_mth d
                    ON d.month_year = k.month_year
                    AND d.country = k.country
                    AND d.site_id = k.site_id
                LEFT JOIN conso_mth c
                    ON c.month_year = k.month_year
                    AND c.country = k.country
                    AND c.site_id = k.site_id
                LEFT JOIN genset_mth g
                    ON g.month_year = k.month_year
                    AND g.country = k.country
                    AND g.site_id = k.site_id
                ORDER BY k.month_year, k.site_id
            """

            params = base_params * 4

            efms = EfmsService()
            conn = efms._open_connection()
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()

            columns = [
                "month_year",
                "country",
                "site_id",
                "site_name",
                "fuel_order_l",
                "fuel_deli_l",
                "fuel_conso_l",
                "ge_working_hours",
                "abnormal_ge_working_hours",
                "monitoring_unavailability_hours",
                "monitoring_unavailability_percent",
            ]

            payloads = []

            for raw in rows:
                row = dict(zip(columns, raw))

                month_year = str(row["month_year"])
                year, month_num = parse_month_year(month_year)

                fuel_order = to_decimal(row["fuel_order_l"])
                fuel_deli = to_decimal(row["fuel_deli_l"])
                fuel_conso = to_decimal(row["fuel_conso_l"])
                ge_hours = to_decimal(row["ge_working_hours"])

                cph = None
                if ge_hours > 0:
                    cph = fuel_conso / ge_hours

                clean = {
                    "month_year": month_year,
                    "year": year,
                    "month": month_num,
                    "country": str(row["country"] or country),
                    "site_id": str(row["site_id"] or "").strip(),
                    "site_name": str(row["site_name"] or "").strip() or None,
                    "fuel_order_l": fuel_order,
                    "fuel_deli_l": fuel_deli,
                    "fuel_conso_l": fuel_conso,
                    "ge_working_hours": ge_hours,
                    "abnormal_ge_working_hours": to_decimal(row["abnormal_ge_working_hours"]),
                    "monitoring_unavailability_hours": to_decimal(row["monitoring_unavailability_hours"]),
                    "monitoring_unavailability_percent": to_decimal(row["monitoring_unavailability_percent"]),
                    "cph_l_per_hour": cph,
                }

                clean["anomaly_flags"] = build_anomalies(clean)
                payloads.append(clean)

            self.stdout.write(f"  Lignes récupérées : {len(payloads)}")

            if dry_run:
                for item in payloads[:10]:
                    self.stdout.write(
                        f"  {item['month_year']} | {item['site_id']} | "
                        f"order={item['fuel_order_l']} | deli={item['fuel_deli_l']} | "
                        f"conso={item['fuel_conso_l']} | ge_h={item['ge_working_hours']} | "
                        f"cph={item['cph_l_per_hour']} | anomalies={item['anomaly_flags']}"
                    )

                sync_run.status = FuelEfmsSyncRun.Status.SUCCESS
                sync_run.rows_fetched = len(payloads)
                sync_run.finished_at = timezone.now()
                sync_run.save()
                self.stdout.write(self.style.SUCCESS("\n  DRY RUN terminé. Aucune donnée insérée.\n"))
                return

            created = 0
            updated = 0

            with transaction.atomic():
                for item in payloads:
                    _, was_created = FuelEfmsMonthly.objects.update_or_create(
                        country=item["country"],
                        month_year=item["month_year"],
                        site_id=item["site_id"],
                        defaults={
                            "year": item["year"],
                            "month": item["month"],
                            "site_name": item["site_name"],
                            "fuel_order_l": item["fuel_order_l"],
                            "fuel_deli_l": item["fuel_deli_l"],
                            "fuel_conso_l": item["fuel_conso_l"],
                            "ge_working_hours": item["ge_working_hours"],
                            "abnormal_ge_working_hours": item["abnormal_ge_working_hours"],
                            "monitoring_unavailability_hours": item["monitoring_unavailability_hours"],
                            "monitoring_unavailability_percent": item["monitoring_unavailability_percent"],
                            "cph_l_per_hour": item["cph_l_per_hour"],
                            "anomaly_flags": item["anomaly_flags"],
                            "synced_at": timezone.now(),
                        },
                    )

                    if was_created:
                        created += 1
                    else:
                        updated += 1

            sync_run.status = FuelEfmsSyncRun.Status.SUCCESS
            sync_run.rows_fetched = len(payloads)
            sync_run.rows_created = created
            sync_run.rows_updated = updated
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.SUCCESS("\n  Synchronisation terminée."))
            self.stdout.write(f"  Créées : {created}")
            self.stdout.write(f"  Mises à jour : {updated}\n")

        except Exception as exc:
            sync_run.status = FuelEfmsSyncRun.Status.FAILED
            sync_run.error_message = str(exc)
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.ERROR(f"\n  Erreur sync eFMS Fuel : {exc}\n"))
            raise