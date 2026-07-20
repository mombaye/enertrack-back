# certification/services/snowflake_service.py
"""
Service de diagnostic Snowflake — étape de faisabilité pour la migration
de la source eFMS (SQL Server) vers Snowflake (DB_GFMS_PROD).

Ne fait aucune écriture, uniquement de la lecture (SHOW / SELECT).
"""
import logging

import snowflake.connector
from django.conf import settings

logger = logging.getLogger(__name__)


class SnowflakeConnectionError(Exception):
    pass


class SnowflakeService:

    def __init__(self):
        self.account          = getattr(settings, "SNOWFLAKE_ACCOUNT", "")
        self.user             = getattr(settings, "SNOWFLAKE_USER", "")
        self.password         = getattr(settings, "SNOWFLAKE_PASSWORD", "")
        self.private_key_path = getattr(settings, "SNOWFLAKE_PRIVATE_KEY_PATH", "")
        self.private_key_pass = getattr(settings, "SNOWFLAKE_PRIVATE_KEY_PASSPHRASE", "")
        self.warehouse        = getattr(settings, "SNOWFLAKE_WAREHOUSE", "")
        self.role             = getattr(settings, "SNOWFLAKE_ROLE", "")
        self.database         = getattr(settings, "SNOWFLAKE_DATABASE", "DB_GFMS_ANALYTICS_PROD")
        self.schema           = getattr(settings, "SNOWFLAKE_SCHEMA", "GOLD")

    def _connect(self):
        kwargs = dict(
            account=self.account,
            user=self.user,
            warehouse=self.warehouse,
            role=self.role,
            database=self.database,
            schema=self.schema,
        )
        if self.private_key_path:
            kwargs["authenticator"]         = "SNOWFLAKE_JWT"
            kwargs["private_key_file"]      = self.private_key_path
            if self.private_key_pass:
                kwargs["private_key_file_pwd"] = self.private_key_pass
        else:
            kwargs["password"] = self.password

        try:
            return snowflake.connector.connect(**kwargs)
        except Exception as e:
            raise SnowflakeConnectionError(f"Connexion Snowflake échouée: {e}") from e

    def check_connection(self) -> bool:
        try:
            conn = self._connect()
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True
        except Exception:
            return False

    def list_schemas(self) -> list[str]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SHOW SCHEMAS IN DATABASE {self.database}")
            return [r[1] for r in cur.fetchall()]
        finally:
            conn.close()

    def list_tables(self, schema: str = None) -> list[str]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SHOW TABLES IN SCHEMA {self.database}.{schema or self.schema}")
            return [r[1] for r in cur.fetchall()]
        finally:
            conn.close()

    def get_table_columns(self, table: str, schema: str = None) -> list[str]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f'SELECT * FROM {self.database}.{schema or self.schema}."{table}" LIMIT 0')
            return [d[0] for d in cur.description]
        finally:
            conn.close()

    def sample_rows(self, table: str, schema: str = None, limit: int = 5) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor(snowflake.connector.DictCursor)
            cur.execute(f'SELECT * FROM {self.database}.{schema or self.schema}."{table}" LIMIT {int(limit)}')
            return cur.fetchall()
        finally:
            conn.close()

    def find_tables_like(self, pattern: str) -> list[tuple[str, str]]:
        """Cherche des tables dont le nom contient `pattern` dans TOUS les schémas de la base."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT table_schema, table_name
                FROM {self.database}.information_schema.tables
                WHERE table_name ILIKE %s
                ORDER BY table_schema, table_name
                """,
                (f"%{pattern}%",),
            )
            return [(r[0], r[1]) for r in cur.fetchall()]
        finally:
            conn.close()

    # ─────────────────────────────────────────────────────────────────────────
    # Requêtes métier — module fuel_tracking (calcul RH)
    # ─────────────────────────────────────────────────────────────────────────

    CHUNK_SIZE = 500

    @staticmethod
    def _chunks(items):
        for i in range(0, len(items), SnowflakeService.CHUNK_SIZE):
            yield items[i:i + SnowflakeService.CHUNK_SIZE]

    def get_data_id_map(self, site_ids: list[str]) -> dict[str, int]:
        """site_id -> DATA_ID via SITE_ESCO_CURRENT (table de mapping)."""
        if not site_ids:
            return {}
        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for chunk in self._chunks(site_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT SITE_ID, DATA_ID
                    FROM {self.database}.{self.schema}.SITE_ESCO_CURRENT
                    WHERE SITE_ID IN ({ph})
                    """,
                    tuple(chunk),
                )
                for site_id, data_id in cur.fetchall():
                    if data_id is not None:
                        result[site_id] = int(data_id)
        finally:
            conn.close()
        return result

    def get_grid_status_map(self, site_ids: list[str]) -> dict[str, str]:
        """site_id -> GRID_SUPPLY_MODIFIED ('On Grid' / 'Off Grid') via SITE_ESCO_CURRENT."""
        if not site_ids:
            return {}
        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for chunk in self._chunks(site_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT SITE_ID, GRID_SUPPLY_MODIFIED
                    FROM {self.database}.{self.schema}.SITE_ESCO_CURRENT
                    WHERE SITE_ID IN ({ph})
                    """,
                    tuple(chunk),
                )
                for site_id, grid_status in cur.fetchall():
                    result[site_id] = grid_status
        finally:
            conn.close()
        return result

    def get_controller_vendor_map(self, site_ids: list[str]) -> dict[str, str]:
        """site_id -> CONTROLLER_VENDOR (ex: 'DeepSea') via SITE_DG."""
        if not site_ids:
            return {}
        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for chunk in self._chunks(site_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT s.SITE_ID, d.CONTROLLER_VENDOR
                    FROM {self.database}.{self.schema}.SITE_ESCO_CURRENT s
                    JOIN {self.database}.{self.schema}.SITE_DG d ON d.S_ID = s.DATA_ID
                    WHERE s.SITE_ID IN ({ph})
                    """,
                    tuple(chunk),
                )
                for site_id, vendor in cur.fetchall():
                    result[site_id] = vendor
        finally:
            conn.close()
        return result

    def get_fuel_level_near_date(self, data_ids: list[int], target_date, window_days: int = 3) -> dict[int, dict]:
        """
        DATA_ID -> {"level": Decimal|None, "timestamp": datetime|None} — dernier
        relevé IM_GENERATOR_FUEL_LEVEL disponible au plus proche de `target_date`
        (recherche dans une fenêtre de ±window_days), utilisé pour Stock Ouv./Clôt. RMS.
        """
        if not data_ids:
            return {}
        from datetime import timedelta

        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            lo = target_date - timedelta(days=window_days)
            hi = target_date + timedelta(days=window_days + 1)
            for chunk in self._chunks(data_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT ID, TIMESTAMP, IM_GENERATOR_FUEL_LEVEL,
                           ABS(DATEDIFF('second', TIMESTAMP, %s)) AS diff_s,
                           ROW_NUMBER() OVER (PARTITION BY ID ORDER BY ABS(DATEDIFF('second', TIMESTAMP, %s))) AS rn
                    FROM {self.database}.{self.schema}.GFMS_METRICS
                    WHERE ID IN ({ph})
                      AND TIMESTAMP >= %s AND TIMESTAMP < %s
                      AND IM_GENERATOR_FUEL_LEVEL IS NOT NULL
                    QUALIFY rn = 1
                    """,
                    (target_date, target_date) + tuple(chunk) + (lo, hi),
                )
                for data_id, ts, level, _diff, _rn in cur.fetchall():
                    result[int(data_id)] = {"level": level, "timestamp": ts}
        finally:
            conn.close()
        return result

    def get_genset_counter_delta(self, data_ids: list[int], date_debut, date_fin) -> dict[int, dict]:
        """DATA_ID -> {"delta": Decimal|None, "nb_jours": int} sur TOTAL_DG_RUNNING_HOUR (compteur cumulatif)."""
        if not data_ids:
            return {}
        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for chunk in self._chunks(data_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT DATA_ID,
                           MAX(TOTAL_DG_RUNNING_HOUR) - MIN(TOTAL_DG_RUNNING_HOUR) AS delta,
                           COUNT(*) AS nb_jours
                    FROM {self.database}.{self.schema}.GENSET_REPORT
                    WHERE DATA_ID IN ({ph})
                      AND REPORT_DATE >= %s AND REPORT_DATE <= %s
                      AND TOTAL_DG_RUNNING_HOUR IS NOT NULL
                    GROUP BY DATA_ID
                    """,
                    tuple(chunk) + (date_debut, date_fin),
                )
                for data_id, delta, nb_jours in cur.fetchall():
                    result[int(data_id)] = {"delta": delta, "nb_jours": int(nb_jours)}
        finally:
            conn.close()
        return result

    def get_status_on_hours(
        self, data_ids: list[int], date_debut, date_fin, status_column: str
    ) -> dict[int, dict]:
        """
        DATA_ID -> {"heures": Decimal|None, "nb_points": int} — compte les points où
        `status_column` (GE_STATUS ou RECTIFIER_STATUS) = 1, converti en heures au
        prorata de la fenêtre temporelle réellement couverte par les points trouvés.
        """
        if status_column not in {"GE_STATUS", "RECTIFIER_STATUS"}:
            raise ValueError(f"Colonne de statut non supportée: {status_column}")
        if not data_ids:
            return {}
        result = {}
        conn = self._connect()
        try:
            cur = conn.cursor()
            for chunk in self._chunks(data_ids):
                ph = ",".join(["%s"] * len(chunk))
                cur.execute(
                    f"""
                    SELECT
                        ID,
                        COUNT(*) AS total_pts,
                        SUM(CASE WHEN TO_VARCHAR({status_column}) = '1' THEN 1 ELSE 0 END) AS on_pts,
                        DATEDIFF('hour', MIN(TIMESTAMP), MAX(TIMESTAMP)) AS span_hours
                    FROM {self.database}.{self.schema}.GFMS_METRICS
                    WHERE ID IN ({ph})
                      AND TIMESTAMP >= %s AND TIMESTAMP < %s
                    GROUP BY ID
                    """,
                    tuple(chunk) + (date_debut, date_fin),
                )
                for data_id, total_pts, on_pts, span_hours in cur.fetchall():
                    heures = None
                    if total_pts and on_pts is not None and span_hours:
                        heures = (on_pts / total_pts) * span_hours
                    result[int(data_id)] = {"heures": heures, "nb_points": int(total_pts or 0)}
        finally:
            conn.close()
        return result

    def diagnose(self) -> dict:
        result = {
            "account": self.account, "user": self.user, "role": self.role,
            "warehouse": self.warehouse, "database": self.database, "schema": self.schema,
            "connected": False, "schemas": None, "tables_in_schema": None,
            "error": None,
        }
        try:
            conn = self._connect()
            result["connected"] = True
            conn.cursor().execute("SELECT 1")
        except Exception as e:
            result["error"] = str(e)
            return result

        try:
            cur = conn.cursor()
            cur.execute(f"SHOW SCHEMAS IN DATABASE {self.database}")
            result["schemas"] = [r[1] for r in cur.fetchall()]

            cur.execute(f"SHOW TABLES IN SCHEMA {self.database}.{self.schema}")
            result["tables_in_schema"] = [r[1] for r in cur.fetchall()]
        except Exception as e:
            result["error"] = f"Introspection: {e}"
        finally:
            conn.close()

        return result
