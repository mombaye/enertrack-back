import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "enertrack_backend.settings")
django.setup()

import calendar
import pandas as pd
from certification.services.snowflake_service import SnowflakeService

YEAR, MONTH = 2026, 7
D_START, D_END = f"{YEAR}-{MONTH:02d}-01", f"{YEAR}-{MONTH:02d}-{calendar.monthrange(YEAR, MONTH)[1]:02d}"
NB_JOURS = calendar.monthrange(YEAR, MONTH)[1]
MIN_PTS, MIN_DENSE = 3, 20

_SOLAR_INVERTER_KW = {"GG_S0": 0.736, "GG_S1": 1.453, "GG_S2": 2.180, "GG_S3": 2.907}
SOLAR_X_PCT = 1.0

def solar_target(typology, load_w, nb_jours):
    if not typology or not nb_jours:
        return None
    t = str(typology).replace(" ", "_").upper()
    inv = next((kw for key, kw in _SOLAR_INVERTER_KW.items() if key in t), None)
    if inv is None:
        return None
    cap = inv * 24 * nb_jours
    if load_w:
        load_kwh = SOLAR_X_PCT * float(load_w) / 1000 * 24 * nb_jours
        return round(min(load_kwh, cap), 3)
    return round(cap, 3)

def delta_pct(actual, ref):
    if actual is None or ref is None or ref == 0:
        return None
    return round((float(actual) / float(ref) - 1) * 100, 1)

sf = SnowflakeService()
conn = sf._connect()
cursor = conn.cursor()
db_schema = f"{sf.database}.{sf.schema}"

cursor.execute(f"""
    WITH site_dim AS (
        SELECT SITE_ID, SITE_NAME, TYPOLOGY, TYPOLOGY_LOAD, DATA_ID
        FROM DB_GFMS_PROD.GOLD.SITES_FILTERED_FIXED
        WHERE COUNTRY = 'Senegal' AND CLIENT = 'AktivCo'
    ),
    grid AS (
        SELECT SITE_ID, SUM(GRID_ENERGY_CONSO_PER_DAY) AS grid_kwh, COUNT(DATE) AS nb_pts
        FROM {db_schema}.GRID_REPORT
        WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s
          AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL AND GRID_ENERGY_CONSO_PER_DAY > 0.1
        GROUP BY SITE_ID
    ),
    acm AS (
        SELECT SITE_ID,
               MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) - MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) AS acm_kwh,
               COUNT(DATE) AS nb_pts
        FROM {db_schema}.AC_METER
        WHERE DATE >= %(d_start)s AND DATE <= %(d_end)s
          AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
        GROUP BY SITE_ID
    ),
    solar AS (
        SELECT sites.SITE_ID, SUM(production.SOLAR_PRODUCTION_KWH_DAY) AS solar_kwh
        FROM DB_GFMS_PROD.GOLD.JOIN_SOLAR_PRODUCTION_AND_MONITORING_AVAILABILITY production
        JOIN DB_GFMS_PROD.GOLD.SITES_FILTERED_FIXED sites ON sites.DATA_ID = production.ID
        WHERE production.DATE >= %(d_start)s AND production.DATE <= %(d_end)s
          AND sites.COUNTRY = 'Senegal' AND sites.CLIENT = 'AktivCo'
        GROUP BY sites.SITE_ID
    )
    SELECT s.SITE_ID, s.TYPOLOGY, s.TYPOLOGY_LOAD, g.grid_kwh, g.nb_pts, a.acm_kwh, a.nb_pts, so.solar_kwh
    FROM site_dim s
    LEFT JOIN grid g ON g.SITE_ID = s.SITE_ID
    LEFT JOIN acm a ON a.SITE_ID = s.SITE_ID
    LEFT JOIN solar so ON so.SITE_ID = s.SITE_ID
    ORDER BY s.SITE_ID
""", {"d_start": D_START, "d_end": D_END})

raw = cursor.fetchall()
conn.close()

rows = []
for site_id, typology, typo_load, grid_kwh, grid_pts, acm_kwh, acm_pts, solar_kwh in raw:
    fms_grid, source = None, "-"
    if grid_kwh is not None and grid_pts and grid_pts >= MIN_PTS:
        fms_grid = float(grid_kwh)
        source = "exact" if grid_pts >= MIN_DENSE else "extrapol"
    fms_acm = float(acm_kwh) if (acm_kwh is not None and acm_pts and acm_pts >= MIN_PTS) else None
    ref_value = fms_grid if fms_grid else (fms_acm if fms_acm else None)
    ref_label = "FMS" if ref_value is not None else "NONE"
    load_w = None
    try:
        load_w = int(float(typo_load)) if typo_load not in (None, "") else None
    except (TypeError, ValueError):
        load_w = None
    s_target = solar_target(typology, load_w, NB_JOURS)
    solar_v = float(solar_kwh) if solar_kwh is not None else None
    rows.append({
        "Site": site_id, "Zone": site_id.split("_")[0], "Periode": f"{YEAR}-{MONTH:02d}",
        "Jours": NB_JOURS, "Conso_facturee": None, "Ref": ref_label,
        "FMS_Grid": fms_grid, "FMS_ACM": fms_acm, "Delta_FMS_Fact": None, "Source": source,
        "Solar_kWh": solar_v, "Solar_Target": s_target, "Delta_Sol_Cible": delta_pct(solar_v, s_target),
        "Conso_estimee": None, "Source_estim": None, "Typologie": typology,
        "Conso_Target": None, "Statut_target": "NO_TARGET", "Delta_vs_Target": None, "Analyse_BO": None,
    })

df = pd.DataFrame(rows)
print(f"\n{len(df)} sites - {YEAR}-{MONTH:02d}\n")
pd.set_option("display.max_rows", 100)
pd.set_option("display.width", 240)
print(df.to_string(index=False))
