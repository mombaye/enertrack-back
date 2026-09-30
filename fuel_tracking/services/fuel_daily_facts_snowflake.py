# fuel_tracking/services/fuel_daily_facts_snowflake.py
"""
Extraction Snowflake (LECTURE SEULE) des faits bruts Suivi Carburant / CPH au
grain (COUNTRY, DATA_ID, DATE) — instruction globale §2 et §3.

Aucune règle métier ici (pas de source retenue, pas de COALESCE à 0) : les
valeurs absentes restent NULL, les doublons contradictoires deviennent NULL
(jamais un choix arbitraire). Le calcul est fait par services/cph_engine.py.

Sources :
  DB_GFMS_ANALYTICS_PROD.GOLD.SITE_ESCO_CURRENT       inventaire (DATA_ID → SITE_ID, réseau, DG_COUNT)
  DB_GFMS_ANALYTICS_PROD.GOLD.GENSET_REPORT           DSE, Day DG On, production GE
  DB_GFMS_PROD.GOLD.GFMS_DATA_TRACKER_NC              compteur horaire (5 min)
  DB_GFMS_PROD.GOLD.RECTIFIER_EFFICIENCY_STATUS       P_DC, rendement, statut (5 min)
  DB_GFMS_ANALYTICS_PROD.GOLD.AC_METER                ACT_ACTIVE_POWER_AVG (W), ACT_ENERGY_P
VW_INVOICE_DATA_REPORT n'est volontairement PAS utilisé (instruction §0).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)

PROD = "DB_GFMS_PROD.GOLD"
ANALYTICS = "DB_GFMS_ANALYTICS_PROD.GOLD"
DATA_ID_CHUNK = 200

# Unités — À CONFIRMER avec `manage.py diagnose_cph_sources` avant la mise en
# production (voir rapport). P_DC est supposée en W (comme LOAD_POWER) et
# RECTIFIER_EFFICIENCY en % (comme la colonne homonyme de GFMS_DATA_TRACKER_NC).
# Surcharge possible par variables d'environnement, sans modifier le code.
P_DC_TO_KW_DIVISOR = Decimal(str(getattr(settings, "FUEL_CPH_P_DC_TO_KW_DIVISOR", "1000")))
EFFICIENCY_TO_RATIO_DIVISOR = Decimal(str(getattr(settings, "FUEL_CPH_EFFICIENCY_TO_RATIO_DIVISOR", "100")))
RECTIFIER_ACTIVE_STATUSES = ("Good", "Aged", "Warning")


def _connect():
    from fuel_tracking.services.fuel_consommation_snowflake import _connect as connect
    return connect()


def _dec(v):
    return None if v is None else Decimal(str(v))


def fetch_inventory(country: str) -> list[dict]:
    """SITE_ESCO_CURRENT pour un pays. Doublons (DATA_ID) contradictoires → valeur NULL."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT DATA_ID,
                   IFF(MIN(SITE_ID) = MAX(SITE_ID), MAX(SITE_ID), NULL) AS site_id,
                   MAX(SITE_NAME) AS site_name,
                   IFF(MIN(GRID_SUPPLY_MODIFIED) = MAX(GRID_SUPPLY_MODIFIED), MAX(GRID_SUPPLY_MODIFIED), NULL) AS grid_supply,
                   IFF(MIN(DG_COUNT) = MAX(DG_COUNT), MAX(DG_COUNT), NULL) AS dg_count
            FROM {ANALYTICS}.SITE_ESCO_CURRENT
            WHERE COUNTRY = %(country)s AND DATA_ID IS NOT NULL
            GROUP BY DATA_ID
        """, {"country": country})
        return [
            {"country": country, "data_id": int(data_id), "site_id": site_id, "site_name": site_name,
             "grid_supply": grid_supply, "dg_count": int(dg_count) if dg_count is not None else None}
            for data_id, site_id, site_name, grid_supply, dg_count in cur.fetchall()
            if site_id is not None
        ]
    finally:
        conn.close()


DAILY_FACTS_SQL = """
WITH
genset AS (
    SELECT DATA_ID AS data_id, REPORT_DATE AS day,
           IFF(MIN(DG_RUNTIME_CONTROLLER) = MAX(DG_RUNTIME_CONTROLLER), MAX(DG_RUNTIME_CONTROLLER), NULL) AS dse_h,
           IFF(MIN(DG_RUNTIME_CALCULATED) = MAX(DG_RUNTIME_CALCULATED), MAX(DG_RUNTIME_CALCULATED), NULL) AS dg_on_h,
           IFF(MIN(DG_PRODUCTION_KWH) = MAX(DG_PRODUCTION_KWH), MAX(DG_PRODUCTION_KWH), NULL) AS prod_kwh
    FROM {analytics}.GENSET_REPORT
    WHERE REPORT_DATE >= %(d_start)s AND REPORT_DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, REPORT_DATE
),
tracker_iv AS (
    SELECT ID AS data_id, "TIMESTAMP" AS ts, CAST("TIMESTAMP" AS DATE) AS day,
           DG_TOTAL_RUNNING_TIME_MINUTES
             - LAG(DG_TOTAL_RUNNING_TIME_MINUTES) OVER (PARTITION BY ID ORDER BY "TIMESTAMP") AS delta_min,
           DATEDIFF('second', LAG("TIMESTAMP") OVER (PARTITION BY ID ORDER BY "TIMESTAMP"), "TIMESTAMP") / 60.0 AS elapsed_min
    FROM {prod}.GFMS_DATA_TRACKER_NC
    WHERE "TIMESTAMP" >= DATEADD('hour', -1, %(d_start)s::TIMESTAMP_NTZ)
      AND "TIMESTAMP" < DATEADD('day', 1, %(d_end)s::TIMESTAMP_NTZ)
      AND ID IN ({ids}) AND DG_TOTAL_RUNNING_TIME_MINUTES IS NOT NULL
),
tracker_ok AS (
    -- Intervalles continus (≤ 10 min) et plausibles (0 ≤ incrément ≤ durée + 1 min)
    SELECT * FROM tracker_iv
    WHERE elapsed_min > 0 AND elapsed_min <= 10
      AND delta_min >= 0 AND delta_min <= elapsed_min + 1
      AND day >= %(d_start)s AND day <= %(d_end)s
),
tracker_daily AS (
    SELECT data_id, day, SUM(delta_min) / 60.0 AS tracker_h, ROUND(SUM(elapsed_min)) AS covered_min,
           COUNT(DISTINCT IFF(delta_min > 0, TIME_SLICE(ts, 5, 'MINUTE'), NULL)) AS ge_on_slots
    FROM tracker_ok GROUP BY data_id, day
),
tracker_on_slots AS (
    SELECT DISTINCT data_id, TIME_SLICE(ts, 5, 'MINUTE') AS slot FROM tracker_ok WHERE delta_min > 0
),
rect_slots AS (
    SELECT ID AS data_id, TIME_SLICE("TIMESTAMP", 5, 'MINUTE') AS slot,
           AVG(P_DC) / %(p_dc_div)s AS p_dc_kw,
           AVG(NULLIF(RECTIFIER_EFFICIENCY, 0)) / %(eff_div)s AS eff,
           MAX(IFF(RECTIFIER_STATUS IN ({active_statuses}), 1, 0)) AS active
    FROM {prod}.RECTIFIER_EFFICIENCY_STATUS
    WHERE "TIMESTAMP" >= %(d_start)s::TIMESTAMP_NTZ
      AND "TIMESTAMP" < DATEADD('day', 1, %(d_end)s::TIMESTAMP_NTZ)
      AND ID IN ({ids})
    GROUP BY ID, TIME_SLICE("TIMESTAMP", 5, 'MINUTE')
),
rect_daily AS (
    SELECT r.data_id, CAST(r.slot AS DATE) AS day,
           COUNT(*) AS slots, SUM(r.active) AS active_slots,
           AVG(IFF(t.slot IS NOT NULL, r.p_dc_kw, NULL)) AS p_dc_ge_tracker_kw,
           AVG(IFF(t.slot IS NOT NULL, r.eff, NULL)) AS eff_ge_tracker,
           AVG(IFF(r.active = 1, r.p_dc_kw, NULL)) AS p_dc_rect_active_kw,
           AVG(IFF(r.active = 1, r.eff, NULL)) AS eff_rect_active,
           AVG(r.p_dc_kw) AS p_dc_day_kw, AVG(r.eff) AS eff_day
    FROM rect_slots r
    LEFT JOIN tracker_on_slots t ON t.data_id = r.data_id AND t.slot = r.slot
    GROUP BY r.data_id, CAST(r.slot AS DATE)
),
ac AS (
    SELECT DATA_ID AS data_id, DATE AS day,
           AVG(ACT_ACTIVE_POWER_AVG) AS ac_w, MAX(ACT_ENERGY_P) AS ac_energy
    FROM {analytics}.AC_METER
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, DATE
),
universe AS (
    SELECT data_id, day FROM genset
    UNION SELECT data_id, day FROM tracker_daily
    UNION SELECT data_id, day FROM rect_daily
    UNION SELECT data_id, day FROM ac
)
SELECT u.data_id, u.day,
       g.dse_h, g.dg_on_h, g.prod_kwh,
       td.tracker_h, td.covered_min, td.ge_on_slots,
       rd.slots, rd.active_slots, rd.p_dc_ge_tracker_kw, rd.eff_ge_tracker,
       rd.p_dc_rect_active_kw, rd.eff_rect_active, rd.p_dc_day_kw, rd.eff_day,
       a.ac_w, a.ac_energy
FROM universe u
LEFT JOIN genset g ON g.data_id = u.data_id AND g.day = u.day
LEFT JOIN tracker_daily td ON td.data_id = u.data_id AND td.day = u.day
LEFT JOIN rect_daily rd ON rd.data_id = u.data_id AND rd.day = u.day
LEFT JOIN ac a ON a.data_id = u.data_id AND a.day = u.day
"""


def build_daily_facts_sql(data_ids: list[int]) -> str:
    # DATA_ID sont des entiers issus de Snowflake (jamais une saisie utilisateur).
    ids = ",".join(str(int(d)) for d in data_ids)
    statuses = ",".join(f"'{s}'" for s in RECTIFIER_ACTIVE_STATUSES)
    return DAILY_FACTS_SQL.format(analytics=ANALYTICS, prod=PROD, ids=ids, active_statuses=statuses)


def fetch_daily_facts(data_ids: list[int], d_start: date, d_end: date) -> list[dict]:
    rows: list[dict] = []
    if not data_ids:
        return rows
    conn = _connect()
    try:
        cur = conn.cursor()
        for i in range(0, len(data_ids), DATA_ID_CHUNK):
            chunk = data_ids[i:i + DATA_ID_CHUNK]
            cur.execute(build_daily_facts_sql(chunk), {
                "d_start": d_start, "d_end": d_end,
                "p_dc_div": float(P_DC_TO_KW_DIVISOR), "eff_div": float(EFFICIENCY_TO_RATIO_DIVISOR),
            })
            for (data_id, day, dse_h, dg_on_h, prod_kwh, tracker_h, covered_min, ge_on_slots,
                 slots, active_slots, p_dc_ge, eff_ge, p_dc_act, eff_act, p_dc_day, eff_day,
                 ac_w, ac_energy) in cur.fetchall():
                rows.append({
                    "data_id": int(data_id), "date": day,
                    "dse_runtime_h": _dec(dse_h), "dg_on_runtime_h": _dec(dg_on_h), "dg_production_kwh": _dec(prod_kwh),
                    "tracker_runtime_h": _dec(tracker_h),
                    "tracker_covered_min": int(covered_min) if covered_min is not None else None,
                    "tracker_ge_on_slots": int(ge_on_slots) if ge_on_slots is not None else None,
                    "rectifier_slots": int(slots) if slots is not None else None,
                    "rectifier_active_slots": int(active_slots) if active_slots is not None else None,
                    "p_dc_ge_tracker_kw": _dec(p_dc_ge), "eff_ge_tracker": _dec(eff_ge),
                    "p_dc_rect_active_kw": _dec(p_dc_act), "eff_rect_active": _dec(eff_act),
                    "p_dc_day_kw": _dec(p_dc_day), "eff_day": _dec(eff_day),
                    "ac_active_power_avg_w": _dec(ac_w), "ac_energy_p": _dec(ac_energy),
                })
    finally:
        conn.close()
    return rows


def date_windows(d_start: date, d_end: date, days: int = 7):
    cur = d_start
    while cur <= d_end:
        end = min(d_end, cur + timedelta(days=days - 1))
        yield cur, end
        cur = end + timedelta(days=1)
