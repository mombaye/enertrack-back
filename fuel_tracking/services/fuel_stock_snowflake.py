# fuel_tracking/services/fuel_stock_snowflake.py
"""
Stock carburant ACTUEL par site depuis Snowflake DB_GFMS_ANALYTICS_DEV.GOLD.
VW_FUEL_REPORT (même vue que fuel_consommation_snowflake.py, mais on lit
LAST_VALID_LEVEL/CAPACITY_L au lieu de ESTIMATED_DROP_VOLUME_L).

Contrairement à la conso mesurée (agrégée sur un mois), le stock est un état
à un instant T. Un jour calendaire fixe (ex: "hier") ne couvre que ~150/483
sites GE Sénégal (vérifié le 2026-08) — beaucoup de sites n'ont pas de ligne
CE jour précis mais en ont une récente. Solution : pour CHAQUE site, prendre
son relevé le plus récent dans une fenêtre glissante de 30 jours (ROW_NUMBER
partitionné par site), ce qui remonte la couverture à 182/483. La date du
relevé (stock_snowflake_date) est conservée pour que l'UI puisse signaler un
stock "périmé" (relevé vieux de plusieurs jours) plutôt que de laisser croire
à une valeur du jour même.
"""
import logging
from datetime import date, timedelta

from django.conf import settings

logger = logging.getLogger(__name__)

FUEL_DATABASE = "DB_GFMS_PROD"
FUEL_SCHEMA = "GOLD"
FUEL_REPORT_DATABASE = "DB_GFMS_ANALYTICS_DEV"

COUNTRY_SCOPE = "Senegal"
WINDOW_DAYS = 30


def _connect():
    import snowflake.connector

    kwargs = dict(
        account=settings.SNOWFLAKE_ACCOUNT,
        user=settings.SNOWFLAKE_USER,
        warehouse=settings.SNOWFLAKE_WAREHOUSE,
        role=settings.SNOWFLAKE_ROLE,
        database=FUEL_DATABASE,
        schema=FUEL_SCHEMA,
    )
    if settings.SNOWFLAKE_PRIVATE_KEY_PATH:
        kwargs["authenticator"] = "SNOWFLAKE_JWT"
        kwargs["private_key_file"] = settings.SNOWFLAKE_PRIVATE_KEY_PATH
        if settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE:
            kwargs["private_key_file_pwd"] = settings.SNOWFLAKE_PRIVATE_KEY_PASSPHRASE
    else:
        kwargs["password"] = settings.SNOWFLAKE_PASSWORD
    return snowflake.connector.connect(**kwargs)


def fetch_stock_snapshot() -> dict[str, dict]:
    """
    Retourne {site_id: {site_name, country, typology, site_type, dg_count,
    power_supply, has_genset, stock_snowflake_l, capacity_snowflake_l,
    stock_snowflake_date, quality_status}} — une entrée pour CHAQUE site du
    périmètre (Sénégal), même sans relevé récent.
    """
    d_start = date.today() - timedelta(days=WINDOW_DAYS)

    conn = _connect()
    try:
        cursor = conn.cursor()
        db_schema = f"{FUEL_DATABASE}.{FUEL_SCHEMA}"
        report_schema = f"{FUEL_REPORT_DATABASE}.{FUEL_SCHEMA}"

        cursor.execute(f"""
            WITH site_dim AS (
                SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY
                FROM (
                    SELECT DATA_ID, SITE_ID, SITE_NAME, COUNTRY, TYPOLOGY, SITE_TYPE, DG_COUNT, POWER_SUPPLY,
                           ROW_NUMBER() OVER (PARTITION BY DATA_ID ORDER BY SITE_ID) AS rn
                    FROM {db_schema}.SITE_FILTERED
                    WHERE COUNTRY = %(country)s
                )
                WHERE rn = 1
            ),
            latest AS (
                SELECT
                    SITE_ID, DATE, LAST_VALID_LEVEL, CAPACITY_L, QUALITY_STATUS,
                    ROW_NUMBER() OVER (PARTITION BY SITE_ID ORDER BY DATE DESC) AS rn
                FROM {report_schema}.VW_FUEL_REPORT
                WHERE COUNTRY = %(country)s AND DATE >= %(d_start)s
                  AND LAST_VALID_LEVEL IS NOT NULL AND CAPACITY_L IS NOT NULL AND CAPACITY_L > 0
            )
            SELECT
                s.SITE_ID, s.SITE_NAME, s.COUNTRY, s.TYPOLOGY, s.SITE_TYPE, s.DG_COUNT, s.POWER_SUPPLY,
                l.DATE, l.LAST_VALID_LEVEL, l.CAPACITY_L, l.QUALITY_STATUS
            FROM site_dim s
            LEFT JOIN latest l ON l.SITE_ID = s.SITE_ID AND l.rn = 1
        """, {"country": COUNTRY_SCOPE, "d_start": d_start})

        result: dict[str, dict] = {}
        for (site_id, site_name, country, typology, site_type, dg_count, power_supply,
             stock_date, level, capacity, quality_status) in cursor.fetchall():
            try:
                has_genset = float(dg_count) > 0 if dg_count is not None else False
            except (TypeError, ValueError):
                has_genset = False
            result[site_id] = {
                "site_name": site_name,
                "country": country,
                "typology": typology,
                "site_type": site_type,
                "dg_count": dg_count,
                "power_supply": power_supply,
                "has_genset": has_genset,
                "stock_snowflake_l": level,
                "capacity_snowflake_l": capacity,
                "stock_snowflake_date": stock_date,
                "quality_status": quality_status,
            }
        return result
    finally:
        conn.close()
