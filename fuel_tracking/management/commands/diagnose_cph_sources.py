# fuel_tracking/management/commands/diagnose_cph_sources.py
"""
Diagnostic LECTURE SEULE des sources Snowflake du calcul CPH, à lancer avant
la mise en production pour confirmer les hypothèses d'unités et de valeurs
(voir fuel_daily_facts_snowflake.py) :

    python manage.py diagnose_cph_sources --start 2026-09-01 --end 2026-09-07
"""
from datetime import date

from django.core.management.base import BaseCommand

from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, PROD, _connect

COLUMNS = {
    f"{ANALYTICS}.SITE_ESCO_CURRENT": ["DATA_ID", "SITE_ID", "COUNTRY", "SITE_NAME", "GRID_SUPPLY_MODIFIED", "DG_COUNT"],
    f"{ANALYTICS}.GENSET_REPORT": ["DATA_ID", "REPORT_DATE", "DG_RUNTIME_CONTROLLER", "DG_RUNTIME_CALCULATED", "DG_PRODUCTION_KWH"],
    f"{PROD}.GFMS_DATA_TRACKER_NC": ["ID", "TIMESTAMP", "DG_TOTAL_RUNNING_TIME_MINUTES", "LOAD_POWER"],
    f"{PROD}.RECTIFIER_EFFICIENCY_STATUS": ["ID", "TIMESTAMP", "P_DC", "P_AC_ACTIVE_POWER", "V_DC_VOLTAGE", "I_DC_CURRENT", "RECTIFIER_EFFICIENCY", "RECTIFIER_STATUS"],
    f"{ANALYTICS}.AC_METER": ["DATA_ID", "DATE", "ACT_ACTIVE_POWER_AVG", "ACT_ENERGY_P"],
    f"{ANALYTICS}.SITE_CABINET_DAILY": ["DATA_ID", "DATE"],
}


class Command(BaseCommand):
    help = "Vérifie colonnes, unités et valeurs des sources Snowflake CPH (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        parser.add_argument("--country", default="Senegal")

    def q(self, cur, sql, params=None):
        try:
            cur.execute(sql, params or {})
            return cur.fetchall()
        except Exception as e:  # noqa: BLE001 — diagnostic : on affiche et on continue
            self.stdout.write(self.style.ERROR(f"    ERREUR : {e}"))
            return None

    def handle(self, *args, **o):
        p = {"s": o["start"], "e": o["end"], "c": o["country"]}
        conn = _connect()
        try:
            cur = conn.cursor()
            self.stdout.write("== Colonnes attendues ==")
            for table, cols in COLUMNS.items():
                db, schema, name = table.split(".")
                rows = self.q(cur, f"SELECT COLUMN_NAME FROM {db}.INFORMATION_SCHEMA.COLUMNS "
                                   "WHERE TABLE_SCHEMA = %(sc)s AND TABLE_NAME = %(t)s", {"sc": schema, "t": name})
                if rows is None:
                    continue
                present = {r[0] for r in rows}
                missing = [c for c in cols if c not in present]
                self.stdout.write(f"  {table} : {len(present)} colonnes ; manquantes : {missing or 'aucune'}")
                if name == "SITE_CABINET_DAILY":
                    self.stdout.write(f"    colonnes disponibles : {sorted(present)}")

            self.stdout.write("\n== Inventaire ==")
            for r in self.q(cur, f"SELECT GRID_SUPPLY_MODIFIED, COUNT(*) FROM {ANALYTICS}.SITE_ESCO_CURRENT "
                                 "WHERE COUNTRY = %(c)s GROUP BY 1 ORDER BY 2 DESC", p) or []:
                self.stdout.write(f"  GRID_SUPPLY_MODIFIED={r[0]!r} : {r[1]}")
            for r in self.q(cur, f"SELECT DG_COUNT, COUNT(*) FROM {ANALYTICS}.SITE_ESCO_CURRENT "
                                 "WHERE COUNTRY = %(c)s GROUP BY 1 ORDER BY 1", p) or []:
                self.stdout.write(f"  DG_COUNT={r[0]!r} : {r[1]}")
            for r in self.q(cur, f"SELECT COUNT(*) FROM (SELECT SITE_ID FROM {ANALYTICS}.SITE_ESCO_CURRENT "
                                 "WHERE COUNTRY = %(c)s AND DG_COUNT > 0 GROUP BY SITE_ID HAVING COUNT(DISTINCT DATA_ID) > 1)", p) or []:
                self.stdout.write(f"  sites avec GE et plusieurs DATA_ID : {r[0]}")

            self.stdout.write("\n== GENSET_REPORT ==")
            for r in self.q(cur, f"""
                SELECT COUNT(*), COUNT(DG_RUNTIME_CONTROLLER), COUNT_IF(DG_RUNTIME_CONTROLLER = 0),
                       COUNT_IF(DG_RUNTIME_CONTROLLER NOT BETWEEN 0 AND 24), COUNT(DG_PRODUCTION_KWH),
                       APPROX_PERCENTILE(DG_PRODUCTION_KWH, 0.5)
                FROM {ANALYTICS}.GENSET_REPORT WHERE REPORT_DATE BETWEEN %(s)s AND %(e)s""", p) or []:
                self.stdout.write(f"  lignes={r[0]} DSE non NULL={r[1]} DSE=0={r[2]} DSE hors 0-24={r[3]} "
                                  f"production non NULL={r[4]} médiane production={r[5]}")
            for r in self.q(cur, f"SELECT COUNT(*) FROM (SELECT DATA_ID, REPORT_DATE FROM {ANALYTICS}.GENSET_REPORT "
                                 "WHERE REPORT_DATE BETWEEN %(s)s AND %(e)s GROUP BY 1, 2 HAVING COUNT(*) > 1)", p) or []:
                self.stdout.write(f"  doublons (DATA_ID, REPORT_DATE) : {r[0]}")

            self.stdout.write("\n== RECTIFIER_EFFICIENCY_STATUS (unités) ==")
            for r in self.q(cur, f"""
                SELECT MIN(P_DC), APPROX_PERCENTILE(P_DC, 0.5), APPROX_PERCENTILE(P_DC, 0.95), MAX(P_DC),
                       MIN(RECTIFIER_EFFICIENCY), APPROX_PERCENTILE(RECTIFIER_EFFICIENCY, 0.5), MAX(RECTIFIER_EFFICIENCY),
                       APPROX_PERCENTILE(V_DC_VOLTAGE * I_DC_CURRENT, 0.5)
                FROM {PROD}.RECTIFIER_EFFICIENCY_STATUS WHERE "TIMESTAMP" >= %(s)s AND "TIMESTAMP" < DATEADD('day', 1, %(e)s)""", p) or []:
                self.stdout.write(f"  P_DC min/p50/p95/max = {r[0]} / {r[1]} / {r[2]} / {r[3]}")
                self.stdout.write(f"  RECTIFIER_EFFICIENCY min/p50/max = {r[4]} / {r[5]} / {r[6]}  (≤ 1 → ratio ; ≤ 100 → %)")
                self.stdout.write(f"  médiane V_DC × I_DC = {r[7]} (comparer à P_DC pour confirmer W ou kW)")
            for r in self.q(cur, f"SELECT RECTIFIER_STATUS, COUNT(*) FROM {PROD}.RECTIFIER_EFFICIENCY_STATUS "
                                 "WHERE \"TIMESTAMP\" >= %(s)s AND \"TIMESTAMP\" < DATEADD('day', 1, %(e)s) GROUP BY 1 ORDER BY 2 DESC", p) or []:
                self.stdout.write(f"  RECTIFIER_STATUS={r[0]!r} : {r[1]}")

            self.stdout.write("\n== AC_METER ==")
            for r in self.q(cur, f"""
                SELECT COUNT(*), COUNT(ACT_ACTIVE_POWER_AVG), MIN(ACT_ACTIVE_POWER_AVG),
                       APPROX_PERCENTILE(ACT_ACTIVE_POWER_AVG, 0.5), MAX(ACT_ACTIVE_POWER_AVG)
                FROM {ANALYTICS}.AC_METER WHERE DATE BETWEEN %(s)s AND %(e)s""", p) or []:
                self.stdout.write(f"  lignes={r[0]} non NULL={r[1]} min/p50/max={r[2]} / {r[3]} / {r[4]} (attendu en W)")
            for r in self.q(cur, f"SELECT COUNT(*) FROM (SELECT DATA_ID, DATE FROM {ANALYTICS}.AC_METER "
                                 "WHERE DATE BETWEEN %(s)s AND %(e)s GROUP BY 1, 2 HAVING COUNT(*) > 1)", p) or []:
                self.stdout.write(f"  lignes multiples par (DATA_ID, DATE) : {r[0]} (moyennées par le calcul)")
        finally:
            conn.close()
