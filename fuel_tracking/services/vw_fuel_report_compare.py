# fuel_tracking/services/vw_fuel_report_compare.py
"""
Comparaison silencieuse Stock RMS (GFMS_DATA_TRACKER_NC, notre calcul) vs.
DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT (vue officielle Snowflake, accès
accordé le 24/07). Purement instrumentation : ne persiste rien en base, ne
change aucun affichage — logge juste les écarts pour accumuler de la
confiance avant un éventuel cutover (vue encore en DEV, gel de construction
jusqu'au 15/08, vraie table PROD attendue en Q4).
"""
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

import snowflake.connector
from django.conf import settings

logger = logging.getLogger(__name__)

DEV_DATABASE = "DB_GFMS_ANALYTICS_DEV"
DEV_SCHEMA = "GOLD"

# Écart toléré avant de logger un warning (au-delà, ratio jugé suspect).
RATIO_LOW = 0.8
RATIO_HIGH = 1.2


def _connect_dev():
    kwargs = dict(
        account=settings.SNOWFLAKE_ACCOUNT,
        user=settings.SNOWFLAKE_USER,
        warehouse=settings.SNOWFLAKE_WAREHOUSE,
        role=settings.SNOWFLAKE_ROLE,
        database=DEV_DATABASE,
        schema=DEV_SCHEMA,
    )
    if settings.SNOWFLAKE_PRIVATE_KEY_PATH:
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
        kwargs["private_key_file"] = settings.SNOWFLAKE_PRIVATE_KEY_PATH
        if settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE:
            kwargs["private_key_file_pwd"] = settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
    else:
        kwargs["password"] = settings.SNOWFLAKE_PASSWORD
    return snowflake.connector.connect(**kwargs)


def _fetch_levels(site_ids: list[str], target_date: date) -> dict[str, Decimal]:
    """
    {site_id: FIRST_VALID_LEVEL} pour target_date, ou le jour valide le plus
    proche dans les ±3 jours si absent ce jour précis (mêmes tolérances que
    notre propre lookup GFMS_DATA_TRACKER_NC).
    """
    if not site_ids:
        return {}
    from datetime import timedelta

    result: dict[str, Decimal] = {}
    conn = _connect_dev()
    try:
        cursor = conn.cursor()
        lo = target_date - timedelta(days=3)
        hi = target_date + timedelta(days=3)
        chunk_size = 500
        for i in range(0, len(site_ids), chunk_size):
            chunk = site_ids[i:i + chunk_size]
            ph = ",".join(["%s"] * len(chunk))
            cursor.execute(f"""
                SELECT SITE_ID, FIRST_VALID_LEVEL,
                       ABS(DATEDIFF('day', DATE, %s)) AS diff_d,
                       ROW_NUMBER() OVER (PARTITION BY SITE_ID ORDER BY ABS(DATEDIFF('day', DATE, %s))) AS rn
                FROM {DEV_DATABASE}.{DEV_SCHEMA}.VW_FUEL_REPORT
                WHERE SITE_ID IN ({ph})
                  AND DATE >= %s AND DATE <= %s
                  AND FIRST_VALID_LEVEL IS NOT NULL
                QUALIFY rn = 1
            """, (target_date, target_date) + tuple(chunk) + (lo, hi))
            for site_id, level, _diff, _rn in cursor.fetchall():
                result[site_id] = level
    finally:
        conn.close()
    return result


def log_stock_rms_comparison(rms_by_site: dict, date_debut: date, date_fin: date) -> None:
    """
    `rms_by_site` : {site_id: {"ouv_rms": Decimal|None, "clot_rms": Decimal|None}}
    déjà calculé par StockRmsService (source GFMS_DATA_TRACKER_NC). Compare à
    VW_FUEL_REPORT pour les mêmes sites/dates et logge les écarts — aucune
    écriture en base, aucun impact sur ce qui est affiché à l'utilisateur.
    """
    site_ids = [sid for sid, v in rms_by_site.items() if v.get("ouv_rms") is not None or v.get("clot_rms") is not None]
    if not site_ids:
        return

    try:
        vwfr_ouv = _fetch_levels(site_ids, date_debut)
        vwfr_clot = _fetch_levels(site_ids, date_fin)
    except Exception as e:
        logger.warning("[vw_fuel_report_compare] Snowflake DEV indisponible, comparaison sautée: %s", e)
        return

    compared = 0
    mismatches = []

    for sid in site_ids:
        ours_ouv = rms_by_site[sid].get("ouv_rms")
        ours_clot = rms_by_site[sid].get("clot_rms")
        theirs_ouv = vwfr_ouv.get(sid)
        theirs_clot = vwfr_clot.get(sid)

        for label, ours, theirs in (("ouv", ours_ouv, theirs_ouv), ("clot", ours_clot, theirs_clot)):
            if ours is None or theirs is None:
                continue
            try:
                ours_f = float(ours)
                theirs_f = float(theirs)
            except (InvalidOperation, TypeError, ValueError):
                continue
            if theirs_f == 0:
                continue
            compared += 1
            ratio = ours_f / theirs_f
            if ratio < RATIO_LOW or ratio > RATIO_HIGH:
                mismatches.append((sid, label, ours_f, theirs_f, ratio))

    logger.info(
        "[vw_fuel_report_compare] %d valeur(s) comparée(s) (GFMS_DATA_TRACKER_NC vs VW_FUEL_REPORT), "
        "%d écart(s) hors tolérance [%.1f, %.1f]",
        compared, len(mismatches), RATIO_LOW, RATIO_HIGH,
    )
    for sid, label, ours_f, theirs_f, ratio in mismatches[:50]:
        logger.warning(
            "[vw_fuel_report_compare] %s/%s : nous=%.1f L, VW_FUEL_REPORT=%.1f L, ratio=%.2f",
            sid, label, ours_f, theirs_f, ratio,
        )
