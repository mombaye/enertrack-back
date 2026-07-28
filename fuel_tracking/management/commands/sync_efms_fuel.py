import logging
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from certification.services.efms import EfmsService
from fuel_tracking.models import FuelEfmsMonthly, FuelEfmsSyncRun, FuelSiteScope
from fuel_tracking.services.rh_service import RhCalculationService
from fuel_tracking.services.rms_service import StockRmsService

logger = logging.getLogger(__name__)


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


def months_between(from_month: str, to_month: str) -> list[str]:
    y1, m1 = parse_month_year(from_month)
    y2, m2 = parse_month_year(to_month)
    start = y1 * 12 + (m1 - 1)
    end = y2 * 12 + (m2 - 1)
    return [f"{(idx // 12):04d}-{(idx % 12) + 1:02d}" for idx in range(start, end + 1)]


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



def deduplicate_payloads(payloads: list[dict]) -> tuple[list[dict], int]:
    """
    Sécurise le bulk upsert PostgreSQL.

    PostgreSQL refuse un bulk_create(update_conflicts=True) si deux lignes du même batch
    ont la même clé unique country/month_year/site_id.

    On fusionne donc les doublons normalisés avant insertion.
    """
    deduped = {}
    duplicates = 0

    sum_fields = [
        "fuel_order_l",
        "fuel_deli_l",
        "fuel_conso_l",
        "ge_working_hours",
        "abnormal_ge_working_hours",
        "monitoring_unavailability_hours",
    ]

    for item in payloads:
        country = str(item.get("country") or "").strip()
        month_year = str(item.get("month_year") or "").strip()
        site_id = str(item.get("site_id") or "").strip()

        if not country or not month_year or not site_id:
            continue

        item["country"] = country
        item["month_year"] = month_year
        item["site_id"] = site_id

        key = (country, month_year, site_id)

        if key not in deduped:
            deduped[key] = item
            continue

        duplicates += 1
        target = deduped[key]

        if not target.get("site_name") and item.get("site_name"):
            target["site_name"] = item["site_name"]

        for field in sum_fields:
            target[field] = to_decimal(target.get(field)) + to_decimal(item.get(field))

        target["monitoring_unavailability_percent"] = max(
            to_decimal(target.get("monitoring_unavailability_percent")),
            to_decimal(item.get("monitoring_unavailability_percent")),
        )

        ge_hours = target["ge_working_hours"]
        if ge_hours > 0:
            target["cph_l_per_hour"] = target["fuel_conso_l"] / ge_hours
        else:
            target["cph_l_per_hour"] = None

        target["anomaly_flags"] = build_anomalies(target)

    return list(deduped.values()), duplicates


def attach_rh_data(payloads: list[dict]) -> None:
    """
    Calcule le RH (cascade Snowflake DSE/redresseur/GE_STATUS, repli ENOC) pour
    chaque (site_id, month_year) présent dans payloads, et injecte rh_hours/
    rh_source/avec_dse directement dans les dicts.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for item in payloads:
        by_month[item["month_year"]].append(item["site_id"])

    rh_svc = RhCalculationService()
    rh_by_key: dict[tuple[str, str], dict] = {}

    for month_year, site_ids in by_month.items():
        year, month = parse_month_year(month_year)
        date_debut = date(year, month, 1)
        date_fin = date(year, month, monthrange(year, month)[1])
        batch_result = rh_svc.compute_rh_batch(site_ids, date_debut, date_fin)
        for site_id, r in batch_result.items():
            rh_by_key[(month_year, site_id)] = r

    for item in payloads:
        r = rh_by_key.get((item["month_year"], item["site_id"]), {})
        item["rh_hours"] = r.get("rh_hours")
        item["rh_source"] = r.get("source")
        item["avec_dse"] = r.get("avec_dse")


def attach_stock_rms_data(payloads: list[dict]) -> None:
    """
    Calcule le Stock Ouv./Clôt. RMS (niveau de cuve télémétrie, au plus proche
    du 1er et du dernier jour du mois) pour chaque (site_id, month_year) présent
    dans payloads, et injecte stock_ouv_rms_l/stock_ouv_rms_at/stock_clot_rms_l/
    stock_clot_rms_at directement dans les dicts.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for item in payloads:
        by_month[item["month_year"]].append(item["site_id"])

    rms_svc = StockRmsService()
    rms_by_key: dict[tuple[str, str], dict] = {}

    for month_year, site_ids in by_month.items():
        year, month = parse_month_year(month_year)
        date_debut = date(year, month, 1)
        date_fin = date(year, month, monthrange(year, month)[1])
        batch_result = rms_svc.compute_rms_batch(site_ids, date_debut, date_fin)
        for site_id, r in batch_result.items():
            rms_by_key[(month_year, site_id)] = r

        # Comparaison silencieuse vs VW_FUEL_REPORT (Snowflake DEV, accès
        # accordé le 24/07) — logge seulement, ne change aucun affichage ni
        # donnée persistée. Sert à accumuler de la confiance avant un éventuel
        # cutover, une fois la vue passée en PROD (attendu Q4).
        try:
            from fuel_tracking.services.vw_fuel_report_compare import log_stock_rms_comparison
            log_stock_rms_comparison(batch_result, date_debut, date_fin)
        except Exception as e:
            logger.warning("[sync_efms_fuel] Comparaison VW_FUEL_REPORT sautée (%s)", e)

    for item in payloads:
        r = rms_by_key.get((item["month_year"], item["site_id"]), {})
        item["stock_ouv_rms_l"] = r.get("ouv_rms")
        item["stock_ouv_rms_at"] = r.get("ouv_rms_at")
        item["stock_clot_rms_l"] = r.get("clot_rms")
        item["stock_clot_rms_at"] = r.get("clot_rms_at")


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

            raw_payload_count = len(payloads)

            # Périmètre : ne garder que les sites avec GE installé (cf. FuelSiteScope).
            # Le parc complet (~3300 sites) inclut des sites On-Grid sans genset qui
            # n'ont rien à consommer côté fuel — les traiter gaspille du calcul RH
            # (Snowflake/ENOC) et pollue le module avec des sites hors-sujet.
            in_scope_ids = set(
                FuelSiteScope.objects.filter(has_genset=True).values_list("site_id", flat=True)
            )
            out_of_scope_count = 0

            if in_scope_ids:
                filtered = [p for p in payloads if p["site_id"] in in_scope_ids]
                out_of_scope_count = len(payloads) - len(filtered)
                payloads = filtered
            else:
                self.stdout.write(self.style.WARNING(
                    "  FuelSiteScope vide — aucun filtrage de périmètre appliqué "
                    "(lancer `import_fuel_site_scope` d'abord)."
                ))

            # ✅ Repli RH / Stock RMS quand eFMS (SQL Server) n'a encore rien publié
            # pour un mois demandé (retard de publication côté eFMS — vu jusqu'à
            # plusieurs mois). RH et Stock RMS viennent de Snowflake/ENOC, pas
            # d'eFMS : sans ce repli, ils ne sont jamais calculés pour ce mois
            # alors qu'ils pourraient l'être indépendamment. On crée des lignes
            # "coquille" (fuel_order/deli/conso à 0) pour les sites du périmètre,
            # uniquement sur les mois demandés n'ayant AUCUNE ligne eFMS — un mois
            # déjà partiellement publié n'est pas touché.
            if in_scope_ids and from_month and to_month:
                covered_months = {p["month_year"] for p in payloads}
                missing_months = [m for m in months_between(from_month, to_month) if m not in covered_months]
                if missing_months:
                    shell_count = 0
                    for m in missing_months:
                        y, mo = parse_month_year(m)
                        for site_id in sorted(in_scope_ids):
                            payloads.append({
                                "month_year": m,
                                "year": y,
                                "month": mo,
                                "country": country,
                                "site_id": site_id,
                                "site_name": None,
                                "fuel_order_l": Decimal("0"),
                                "fuel_deli_l": Decimal("0"),
                                "fuel_conso_l": Decimal("0"),
                                "ge_working_hours": Decimal("0"),
                                "abnormal_ge_working_hours": Decimal("0"),
                                "monitoring_unavailability_hours": Decimal("0"),
                                "monitoring_unavailability_percent": Decimal("0"),
                                "cph_l_per_hour": None,
                                "anomaly_flags": [],
                            })
                            shell_count += 1
                    self.stdout.write(self.style.WARNING(
                        f"  eFMS (SQL Server) n'a rien publié pour {len(missing_months)} mois "
                        f"({', '.join(missing_months)}) — {shell_count} ligne(s) coquille ajoutée(s) "
                        "pour calculer RH/Stock RMS quand même (Snowflake/ENOC, indépendant d'eFMS)."
                    ))

            payloads, duplicate_count = deduplicate_payloads(payloads)

            self.stdout.write(f"  Lignes récupérées SQL      : {raw_payload_count}")
            if in_scope_ids:
                self.stdout.write(f"  Hors périmètre (sans GE)   : {out_of_scope_count}")
            self.stdout.write(f"  Lignes uniques préparées   : {len(payloads)}")

            if duplicate_count:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Doublons normalisés fusionnés : {duplicate_count}"
                    )
                )

            self.stdout.write("  Calcul RH (Snowflake / ENOC)...")
            attach_rh_data(payloads)

            self.stdout.write("  Calcul Stock Ouv./Clôt. RMS (Snowflake)...")
            attach_stock_rms_data(payloads)

            if dry_run:
                for item in payloads[:10]:
                    self.stdout.write(
                        f"  {item['month_year']} | {item['site_id']} | "
                        f"order={item['fuel_order_l']} | deli={item['fuel_deli_l']} | "
                        f"conso={item['fuel_conso_l']} | ge_h={item['ge_working_hours']} | "
                        f"cph={item['cph_l_per_hour']} | anomalies={item['anomaly_flags']} | "
                        f"rh={item['rh_hours']} [{item['rh_source']}] avec_dse={item['avec_dse']} | "
                        f"stock_ouv_rms={item['stock_ouv_rms_l']} stock_clot_rms={item['stock_clot_rms_l']}"
                    )

                sync_run.status = FuelEfmsSyncRun.Status.SUCCESS
                sync_run.rows_fetched = raw_payload_count
                sync_run.finished_at = timezone.now()
                sync_run.save()
                self.stdout.write(self.style.SUCCESS("\n  DRY RUN terminé. Aucune donnée insérée.\n"))
                return

            created = 0
            updated = 0

            now = timezone.now()

            existing_keys = set(
                FuelEfmsMonthly.objects.filter(
                    country=country,
                    month_year__gte=from_month,
                    month_year__lte=to_month,
                ).values_list("country", "month_year", "site_id")
            )

            objects = []

            for item in payloads:
                key = (item["country"], item["month_year"], item["site_id"])

                if key in existing_keys:
                    updated += 1
                else:
                    created += 1

                objects.append(
                    FuelEfmsMonthly(
                        country=item["country"],
                        month_year=item["month_year"],
                        site_id=item["site_id"],
                        year=item["year"],
                        month=item["month"],
                        site_name=item["site_name"],
                        fuel_order_l=item["fuel_order_l"],
                        fuel_deli_l=item["fuel_deli_l"],
                        fuel_conso_l=item["fuel_conso_l"],
                        ge_working_hours=item["ge_working_hours"],
                        abnormal_ge_working_hours=item["abnormal_ge_working_hours"],
                        monitoring_unavailability_hours=item["monitoring_unavailability_hours"],
                        monitoring_unavailability_percent=item["monitoring_unavailability_percent"],
                        cph_l_per_hour=item["cph_l_per_hour"],
                        anomaly_flags=item["anomaly_flags"],
                        rh_hours=item["rh_hours"],
                        rh_source=item["rh_source"],
                        avec_dse=item["avec_dse"],
                        stock_ouv_rms_l=item["stock_ouv_rms_l"],
                        stock_ouv_rms_at=item["stock_ouv_rms_at"],
                        stock_clot_rms_l=item["stock_clot_rms_l"],
                        stock_clot_rms_at=item["stock_clot_rms_at"],
                        synced_at=now,
                    )
                )

            if not dry_run and objects:
                with transaction.atomic():
                    FuelEfmsMonthly.objects.bulk_create(
                        objects,
                        batch_size=1000,
                        update_conflicts=True,
                        unique_fields=["country", "month_year", "site_id"],
                        update_fields=[
                            "year",
                            "month",
                            "site_name",
                            "fuel_order_l",
                            "fuel_deli_l",
                            "fuel_conso_l",
                            "ge_working_hours",
                            "abnormal_ge_working_hours",
                            "monitoring_unavailability_hours",
                            "monitoring_unavailability_percent",
                            "cph_l_per_hour",
                            "anomaly_flags",
                            "rh_hours",
                            "rh_source",
                            "avec_dse",
                            "stock_ouv_rms_l",
                            "stock_ouv_rms_at",
                            "stock_clot_rms_l",
                            "stock_clot_rms_at",
                            "synced_at",
                        ],
                    )

            sync_run.status = FuelEfmsSyncRun.Status.SUCCESS
            sync_run.rows_fetched = raw_payload_count
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