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
  DB_GFMS_PROD.GOLD.RECTIFIER_EFFICIENCY_STATUS       P_DC, rendement, statut (5 min) ; puissance redresseur
                                                      P_DC ÷ (rendement/100) ÷ 1000 (calcul validé équipe data, 23/11/2026)
  DB_GFMS_ANALYTICS_PROD.GOLD.AC_METER                ACT_ACTIVE_POWER_AVG (W), ACT_ENERGY_P
  DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT           conso MESURÉE du jour (même filtre strict que la conso mensuelle)
  DB_GFMS_PROD.GOLD.AVERAGE_LOAD                      LOAD_W : load DC telco moyen du jour (W)
  DB_GFMS_ANALYTICS_PROD.GOLD.BATTERY_REPORT          Ah chargés / déchargés du jour ; recharge batterie calculée par
                                                      l'équipe data (24/11/2026) : médiane du jour de courant × tension
                                                      ÷ 1000 sur les mesures en charge, pas reçus, pas en charge
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
FUEL_REPORT = "DB_GFMS_ANALYTICS_DEV.GOLD"  # VW_FUEL_REPORT (cf. fuel_consommation_snowflake.py)
# Recharge batterie (équipe data, GOLD.BATTERY_REPORT) : médiane du jour fournie par la table ; jour qualifié si pas de
# 5 min reçus (TELEMETRY_RECEIVED_COUNT) ≥ 50 % de 288.
# Puissance redresseur (règle équipe data) : mesure valide si statut Good/Aged/Warning, P_DC > 0, rendement 70-100 % ;
# jour qualifié si mesures valides ≥ 50 % de 288 pas de 5 min. Mesures des jours NON qualifiés conservées (repli période).
RECT_POWER_EFF_MIN, RECT_POWER_EFF_MAX = 70, 100
RECT_POWER_DAY_SLOTS = 288
RECT_POWER_QUALIFIED_RATIO = 0.5
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


def _values(v) -> list[float] | None:
    """ARRAY Snowflake (texte JSON côté connecteur) → liste de float ; None si absent."""
    if v is None:
        return None
    if isinstance(v, str):
        import json

        v = json.loads(v)
    return [float(x) for x in v if x is not None]


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
    SELECT DATA_ID AS data_id, DATE AS day,
           IFF(MIN(DG_RUNTIME_CONTROLLER) = MAX(DG_RUNTIME_CONTROLLER), MAX(DG_RUNTIME_CONTROLLER), NULL) AS dse_h,
           IFF(MIN(DG_RUNTIME_CALCULATED) = MAX(DG_RUNTIME_CALCULATED), MAX(DG_RUNTIME_CALCULATED), NULL) AS dg_on_h,
           IFF(MIN(DG_PRODUCTION_KWH) = MAX(DG_PRODUCTION_KWH), MAX(DG_PRODUCTION_KWH), NULL) AS prod_kwh,
           IFF(MIN(TOTAL_DG_RUNNING_HOUR) = MAX(TOTAL_DG_RUNNING_HOUR), MAX(TOTAL_DG_RUNNING_HOUR), NULL) AS total_h
    FROM {analytics}.GENSET_REPORT
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, DATE
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
           AVG(ACT_ACTIVE_POWER_AVG) AS ac_w, MAX(ACT_ENERGY_P) AS ac_energy,
           COUNT(ACT_ACTIVE_POWER_AVG) AS ac_points
    FROM {analytics}.AC_METER
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, DATE
),
fuel AS (
    -- Conso MESURÉE (capteur de cuve). Même filtre que la conso mensuelle :
    -- QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2. Baisse détectée → volume
    -- de la baisse ; ni baisse ni remplissage → 0 L mesuré ; autre cas (qualité
    -- insuffisante, remplissage sans baisse, drapeau NULL, doublon) → NULL.
    SELECT DATA_ID AS data_id, DATE AS day,
           IFF(COUNT(*) = 1, MAX(CASE
               WHEN QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2 AND DROP_DETECTED = TRUE
                   THEN ESTIMATED_DROP_VOLUME_L
               WHEN QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2 AND DROP_DETECTED = FALSE AND REFILL_DETECTED = FALSE
                   THEN 0
           END), NULL) AS measured_l,
           SUM(RAW_POINT_COUNT) AS raw_points,
           IFF(MIN(LAST_VALID_LEVEL) = MAX(LAST_VALID_LEVEL), MAX(LAST_VALID_LEVEL), NULL) AS fms_level
    FROM {fuel_report}.VW_FUEL_REPORT
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
      AND (%(country)s IS NULL OR COUNTRY = %(country)s)
    GROUP BY DATA_ID, DATE
),
load_dc AS (
    -- Load DC telco moyen du jour (W). Doublon contradictoire → NULL.
    SELECT ID AS data_id, DATE AS day, IFF(MIN(LOAD_W) = MAX(LOAD_W), MAX(LOAD_W), NULL) AS load_w
    FROM {prod}.AVERAGE_LOAD
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND ID IN ({ids})
    GROUP BY ID, DATE
),
battery AS (
    -- Ah chargés / déchargés du jour (BATTERY_REPORT). Doublon contradictoire → NULL.
    SELECT DATA_ID AS data_id, DATE AS day,
           IFF(MIN(BATTERY_CHARGE_CURRENT_DAY_AH) = MAX(BATTERY_CHARGE_CURRENT_DAY_AH), MAX(BATTERY_CHARGE_CURRENT_DAY_AH), NULL) AS charge_ah,
           IFF(MIN(BATTERY_DISCHARGE_CURRENT_DAY_AH) = MAX(BATTERY_DISCHARGE_CURRENT_DAY_AH), MAX(BATTERY_DISCHARGE_CURRENT_DAY_AH), NULL) AS discharge_ah
    FROM {analytics}.BATTERY_REPORT
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, DATE
),
batt_daily AS (
    -- Recharge batterie (équipe data) : BATTERY_CHARGE_POWER_MEDIAN_KW = médiane du jour de courant × tension ÷ 1000 sur les
    -- mesures en charge (NULL si aucune charge, jamais 0). Pas reçus ≠ pas en charge. Doublon contradictoire → NULL.
    -- Courant / tension médians : non fournis par la table (NULL).
    SELECT DATA_ID AS data_id, DATE AS day,
           IFF(MIN(TELEMETRY_RECEIVED_COUNT) = MAX(TELEMETRY_RECEIVED_COUNT), MAX(TELEMETRY_RECEIVED_COUNT), NULL) AS batt_points,
           IFF(MIN(BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT) = MAX(BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT),
               MAX(BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT), NULL) AS batt_pos,
           NULL::FLOAT AS batt_i_med, NULL::FLOAT AS batt_v_med,
           IFF(MIN(BATTERY_CHARGE_POWER_MEDIAN_KW) = MAX(BATTERY_CHARGE_POWER_MEDIAN_KW), MAX(BATTERY_CHARGE_POWER_MEDIAN_KW), NULL) AS batt_kw_med,
           NULL::ARRAY AS batt_values
    FROM {analytics}.BATTERY_REPORT
    WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s AND DATA_ID IN ({ids})
    GROUP BY DATA_ID, DATE
),
rect_power_valid AS (
    -- Puissance redresseur par mesure (kW) = P_DC (W) ÷ (RECTIFIER_EFFICIENCY en pourcentage / 100) ÷ 1000, AVANT toute médiane
    SELECT ID AS data_id, CAST("TIMESTAMP" AS DATE) AS day,
           P_DC / (RECTIFIER_EFFICIENCY / 100.0) / 1000.0 AS rect_kw
    FROM {prod}.RECTIFIER_EFFICIENCY_STATUS
    WHERE "TIMESTAMP" >= %(d_start)s::TIMESTAMP_NTZ
      AND "TIMESTAMP" < DATEADD('day', 1, %(d_end)s::TIMESTAMP_NTZ)
      AND ID IN ({ids})
      AND RECTIFIER_STATUS IN ({active_statuses})
      AND P_DC > 0
      AND RECTIFIER_EFFICIENCY BETWEEN {eff_min} AND {eff_max}
),
rect_power_daily AS (
    SELECT data_id, day, COUNT(*) AS rect_power_points, MEDIAN(rect_kw) AS rect_power_median_kw,
           IFF(COUNT(*) < {day_slots} * {qualified_ratio}, ARRAY_AGG(ROUND(rect_kw, 4)), NULL) AS rect_power_values
    FROM rect_power_valid GROUP BY data_id, day
),
universe AS (
    SELECT data_id, day FROM genset
    UNION SELECT data_id, day FROM tracker_daily
    UNION SELECT data_id, day FROM rect_daily
    UNION SELECT data_id, day FROM ac
    UNION SELECT data_id, day FROM fuel
    UNION SELECT data_id, day FROM load_dc
    UNION SELECT data_id, day FROM battery
    UNION SELECT data_id, day FROM batt_daily
    UNION SELECT data_id, day FROM rect_power_daily
)
SELECT u.data_id, u.day,
       g.dse_h, g.dg_on_h, g.prod_kwh,
       td.tracker_h, td.covered_min, td.ge_on_slots,
       rd.slots, rd.active_slots, rd.p_dc_ge_tracker_kw, rd.eff_ge_tracker,
       rd.p_dc_rect_active_kw, rd.eff_rect_active, rd.p_dc_day_kw, rd.eff_day,
       a.ac_w, a.ac_energy, a.ac_points,
       f.measured_l, f.raw_points, g.total_h, f.fms_level,
       ld.load_w, b.charge_ah, b.discharge_ah, bd.batt_points, bd.batt_pos, bd.batt_i_med, bd.batt_v_med,
       bd.batt_kw_med, bd.batt_values,
       rp.rect_power_points, rp.rect_power_median_kw, rp.rect_power_values
FROM universe u
LEFT JOIN genset g ON g.data_id = u.data_id AND g.day = u.day
LEFT JOIN tracker_daily td ON td.data_id = u.data_id AND td.day = u.day
LEFT JOIN rect_daily rd ON rd.data_id = u.data_id AND rd.day = u.day
LEFT JOIN ac a ON a.data_id = u.data_id AND a.day = u.day
LEFT JOIN fuel f ON f.data_id = u.data_id AND f.day = u.day
LEFT JOIN load_dc ld ON ld.data_id = u.data_id AND ld.day = u.day
LEFT JOIN battery b ON b.data_id = u.data_id AND b.day = u.day
LEFT JOIN batt_daily bd ON bd.data_id = u.data_id AND bd.day = u.day
LEFT JOIN rect_power_daily rp ON rp.data_id = u.data_id AND rp.day = u.day
"""


# Compteur horaire indisponible (vue GFMS_DATA_TRACKER_NC en erreur côté Snowflake) : mêmes colonnes, aucune
# ligne → tracker_h / covered_min / ge_on_slots restent NULL (absent, jamais 0) et les autres sources passent.
NO_TRACKER_CTES = """tracker_daily AS (
    SELECT NULL::NUMBER AS data_id, NULL::DATE AS day, NULL::FLOAT AS tracker_h,
           NULL::NUMBER AS covered_min, NULL::NUMBER AS ge_on_slots
    WHERE FALSE
),
tracker_on_slots AS (
    SELECT NULL::NUMBER AS data_id, NULL::TIMESTAMP_NTZ AS slot WHERE FALSE
),
"""


def build_daily_facts_sql(data_ids: list[int], with_tracker: bool = True) -> str:
    # DATA_ID sont des entiers issus de Snowflake (jamais une saisie utilisateur).
    ids = ",".join(str(int(d)) for d in data_ids)
    statuses = ",".join(f"'{s}'" for s in RECTIFIER_ACTIVE_STATUSES)
    sql = DAILY_FACTS_SQL
    if not with_tracker:
        a, b = sql.index("tracker_iv AS ("), sql.index("rect_slots AS (")
        sql = sql[:a] + NO_TRACKER_CTES + sql[b:]
    return sql.format(analytics=ANALYTICS, prod=PROD, fuel_report=FUEL_REPORT, ids=ids,
                      active_statuses=statuses,
                      eff_min=RECT_POWER_EFF_MIN, eff_max=RECT_POWER_EFF_MAX, day_slots=RECT_POWER_DAY_SLOTS,
                      qualified_ratio=RECT_POWER_QUALIFIED_RATIO)


def view_status(cur, view: str) -> str | None:
    """None si la vue est lisible, sinon le message d'erreur Snowflake (lecture seule, aucune ligne lue)."""
    try:
        cur.execute(f"SELECT 1 FROM {view} LIMIT 0")
        return None
    except Exception as e:  # erreur de compilation de la vue, droit manquant…
        return str(e).splitlines()[0][:300]


def tracker_status(cur) -> str | None:
    """None si GFMS_DATA_TRACKER_NC est lisible, sinon le message d'erreur Snowflake (lecture seule, aucune ligne lue)."""
    return view_status(cur, f"{PROD}.GFMS_DATA_TRACKER_NC")


def fetch_daily_facts(data_ids: list[int], d_start: date, d_end: date, country: str | None = None,
                      warnings: list | None = None) -> list[dict]:
    """Faits journaliers. Si le compteur horaire est illisible, la synchro continue sans lui et l'avertissement
    est ajouté à `warnings` (l'appelant le journalise dans le run) : jamais un échec silencieux ni un 0."""
    rows: list[dict] = []
    if not data_ids:
        return rows
    conn = _connect()
    try:
        cur = conn.cursor()
        tracker_err = tracker_status(cur)
        if tracker_err:
            msg = f"Compteur horaire GFMS_DATA_TRACKER_NC illisible, heures compteur laissées absentes : {tracker_err}"
            logger.warning("[fuel_daily_facts] %s", msg)
            if warnings is not None and msg not in warnings:
                warnings.append(msg)
        for i in range(0, len(data_ids), DATA_ID_CHUNK):
            chunk = data_ids[i:i + DATA_ID_CHUNK]
            cur.execute(build_daily_facts_sql(chunk, with_tracker=tracker_err is None), {
                "d_start": d_start, "d_end": d_end, "country": country,
                "p_dc_div": float(P_DC_TO_KW_DIVISOR), "eff_div": float(EFFICIENCY_TO_RATIO_DIVISOR),
            })
            for (data_id, day, dse_h, dg_on_h, prod_kwh, tracker_h, covered_min, ge_on_slots,
                 slots, active_slots, p_dc_ge, eff_ge, p_dc_act, eff_act, p_dc_day, eff_day,
                 ac_w, ac_energy, ac_points, measured_l, raw_points, total_h, fms_level,
                 load_w, charge_ah, discharge_ah, b_pts, b_pos, b_i, b_v, b_kw, b_vals,
                 rect_pts, rect_med, rect_vals) in cur.fetchall():
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
                    "ac_point_count": int(ac_points) if ac_points is not None else None,
                    "measured_conso_l": _dec(measured_l),
                    "fuel_raw_points": int(raw_points) if raw_points is not None else None,
                    "dg_total_running_h": _dec(total_h), "fms_level_l": _dec(fms_level),
                    "load_dc_w": _dec(load_w), "battery_charge_ah": _dec(charge_ah),
                    "battery_discharge_ah": _dec(discharge_ah),
                    "battery_telemetry_points": int(b_pts) if b_pts is not None else None,
                    "battery_charge_slots": int(b_pos) if b_pos is not None else None,
                    "battery_charge_median_current_a": _dec(b_i), "battery_charge_median_voltage_v": _dec(b_v),
                    "battery_charge_median_kw": _dec(b_kw), "battery_charge_values_kw": _values(b_vals),
                    "rect_power_points": int(rect_pts) if rect_pts is not None else None,
                    "rect_power_median_kw": _dec(rect_med),
                    "rect_power_values_kw": _values(rect_vals),
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
