# certification/services/efms.py
# v3 — get_conso_acm() utilise SUM(act_energy_p) au lieu du calcul U×I

import time
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pyodbc
from django.conf import settings

logger = logging.getLogger(__name__)


class EfmsConnectionError(Exception):
    pass

class EfmsQueryError(Exception):
    pass


class EfmsService:

    TABLE_GRID = "[SQL1-ProdDB].[dbo].[silver.gfms_Grid_Report_day]"
    TABLE_ACM  = "[SQL1-ProdDB].[dbo].[silver.gfms_AC_Meter_Report_day]"

    # Colonne énergie Grid (résolue dynamiquement)
    _COL_CONSO_CANDIDATES = [
        "Grid Energy Conso per Day",
        "Grid_Energy_Conso_per_day",
        "Grid_Energy_Conso_per_Day",
        "GridEnergyConso",
        "energy_kwh",
        "conso_kwh",
    ]
    _col_conso_resolved: Optional[str] = None

    # Seuil minimum de jours pour considérer un mois "complet"
    MIN_DAYS_DENSE      = 20
    # Seuil minimum de points pour extrapoler
    MIN_POINTS_EXTRAPOL = 3

    # ── Colonnes ACM énergie active (triphasé ACT) ────────────────────────────
    # act_energy_p  : compteur principal — énergie active kWh/jour
    # act2_energy_p : compteur secondaire (fallback si act_energy_p absent)
    COL_ACM_ENERGY_PRIMARY   = "act_energy_p"
    COL_ACM_ENERGY_SECONDARY = "act2_energy_p"

    def __init__(self, cert_batch=None):
        self.cert_batch  = cert_batch
        self.host        = getattr(settings, "EFMS_SQL_HOST",      "172.30.0.149")
        self.port        = int(getattr(settings, "EFMS_SQL_PORT",   1433))
        self.db          = getattr(settings, "EFMS_SQL_DB",        "SQL1-ProdDB")
        self.user        = getattr(settings, "EFMS_SQL_USER",      "")
        self.password    = getattr(settings, "EFMS_SQL_PASSWORD",  "")
        self.driver      = getattr(settings, "EFMS_SQL_DRIVER",    "ODBC Driver 17 for SQL Server")
        self.timeout     = int(getattr(settings, "EFMS_SQL_TIMEOUT",     10))
        self.max_retries = int(getattr(settings, "EFMS_SQL_MAX_RETRIES", 2))

        col_override = getattr(settings, "EFMS_SQL_COL_CONSO", None)
        if col_override:
            EfmsService._col_conso_resolved = col_override

    # ── Résolution colonne Grid ────────────────────────────────────────────────

    def _resolve_col_conso(self, conn) -> str:
        if EfmsService._col_conso_resolved:
            return EfmsService._col_conso_resolved

        cursor = conn.cursor()
        cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
        columns = [d[0] for d in cursor.description]
        logger.info(f"[eFMS] Colonnes Grid disponibles: {columns}")

        columns_lower = {c.lower(): c for c in columns}
        for candidate in self._COL_CONSO_CANDIDATES:
            match = columns_lower.get(candidate.lower())
            if match:
                EfmsService._col_conso_resolved = match
                logger.info(f"[eFMS] Colonne résolue → [{match}]")
                return match

        for col in columns:
            if any(k in col.lower() for k in ("conso", "energy", "kwh")):
                EfmsService._col_conso_resolved = col
                logger.warning(f"[eFMS] Colonne heuristique → [{col}]")
                return col

        raise EfmsQueryError(
            f"Colonne énergie introuvable. Colonnes: {columns}. "
            f"→ Définir EFMS_SQL_COL_CONSO dans .env"
        )

    def _col(self, conn) -> str:
        return f"[{self._resolve_col_conso(conn)}]"

    # ── Connexion ──────────────────────────────────────────────────────────────

    def _build_conn_string(self) -> str:
        return (
            f"DRIVER={{{self.driver}}};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.db};"
            f"UID={self.user};"
            f"PWD={self.password};"
            f"Connection Timeout={self.timeout};"
            "TrustServerCertificate=yes;"
        )

    def _open_connection(self):
        from certification.models import EfmsConnectionLog

        last_error = None
        for attempt in range(1, self.max_retries + 1):
            t0 = time.monotonic()
            try:
                conn = pyodbc.connect(self._build_conn_string(), timeout=self.timeout)
                duration_ms = int((time.monotonic() - t0) * 1000)
                EfmsConnectionLog.objects.create(
                    cert_batch=self.cert_batch,
                    status=EfmsConnectionLog.Status.SUCCESS,
                    duration_ms=duration_ms, host=self.host,
                )
                return conn
            except pyodbc.OperationalError as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                error_msg = str(e)
                last_error = e
                log_status = (EfmsConnectionLog.Status.TIMEOUT
                              if "timeout" in error_msg.lower()
                              else EfmsConnectionLog.Status.VPN_DOWN)
                logger.warning(f"[eFMS] Tentative {attempt}/{self.max_retries}: {error_msg[:200]}")
                EfmsConnectionLog.objects.create(
                    cert_batch=self.cert_batch, status=log_status,
                    duration_ms=duration_ms, host=self.host, error=error_msg[:500],
                )
            except pyodbc.Error as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                error_msg = str(e)
                last_error = e
                EfmsConnectionLog.objects.create(
                    cert_batch=self.cert_batch,
                    status=EfmsConnectionLog.Status.SQL_ERROR,
                    duration_ms=duration_ms, host=self.host, error=error_msg[:500],
                )
            if attempt < self.max_retries:
                time.sleep(2 * attempt)

        raise EfmsConnectionError(
            f"Impossible de joindre eFMS après {self.max_retries} tentatives: {last_error}"
        )

    # ── Helpers requête ACM ────────────────────────────────────────────────────

    def _query_acm_energy(
        self,
        cursor,
        site_id: str,
        date_debut: date,
        date_fin: date,
        col: str,
    ) -> tuple[Optional[Decimal], int]:
        """
        Retourne (conso_kwh, nb_points) pour la colonne ACM demandée.

        act_energy_p est un INDEX CUMULATIF (compteur kWh monotone croissant),
        pas une consommation journalière. La consommation sur une période est :

            conso = MAX(act_energy_p) - MIN(act_energy_p)

        Pour les jours manquants (NULL) l'index est interpolé via les voisins,
        donc MAX-MIN reste valide tant qu'il y a au moins 2 points non-NULL.

        Logique adaptive :
          - dense (≥20 pts) → MAX-MIN exact
          - épars (≥3 pts)  → moy_delta_journalier × nb_jours_periode
          - très épars      → fenêtre ±90j pour estimer la moy_delta
        """
        nb_jours_periode = (date_fin - date_debut).days + 1

        cursor.execute(f"""
            SELECT
                MAX([{col}])   AS idx_max,
                MIN([{col}])   AS idx_min,
                COUNT([Date])  AS nb_pts
            FROM {self.TABLE_ACM}
            WHERE site_id = ?
              AND [Date] >= ? AND [Date] <= ?
              AND [{col}] IS NOT NULL AND [{col}] > 0
        """, (site_id, date_debut, date_fin))
        row = cursor.fetchone()

        if row and row[0] is not None and row[2]:
            nb   = int(row[2])
            conso = Decimal(str(row[0])) - Decimal(str(row[1]))  # MAX - MIN

            if conso <= Decimal("0"):
                # Index ne monte pas → données suspectes (reset compteur ?)
                logger.warning(
                    "[eFMS ACM] %s : delta=%.3f ≤ 0 sur %s→%s (%d pts) — ignoré",
                    site_id, float(conso), date_debut, date_fin, nb,
                )
                return None, 0

            if nb >= self.MIN_DAYS_DENSE:
                # Dense : MAX-MIN fiable
                return conso, nb

            if nb >= self.MIN_POINTS_EXTRAPOL:
                # Épars : extrapoler depuis la moy journalière observée
                # On divise par (nb-1) car la différence couvre nb-1 intervalles
                intervals = max(1, nb - 1)
                moy_jour  = conso / Decimal(str(intervals))
                extrapol  = moy_jour * Decimal(str(nb_jours_periode))
                return extrapol, nb

        # Fenêtre ±90j pour estimer la moy journalière
        f_debut = date_debut - timedelta(days=90)
        f_fin   = date_fin   + timedelta(days=90)
        cursor.execute(f"""
            SELECT
                MAX([{col}])  AS idx_max,
                MIN([{col}])  AS idx_min,
                COUNT([Date]) AS nb_pts,
                DATEDIFF(day, MIN([Date]), MAX([Date])) AS nb_days_span
            FROM {self.TABLE_ACM}
            WHERE site_id = ?
              AND [Date] >= ? AND [Date] <= ?
              AND [{col}] IS NOT NULL AND [{col}] > 0
        """, (site_id, f_debut, f_fin))
        row2 = cursor.fetchone()

        if (row2 and row2[0] is not None and row2[2]
                and int(row2[2]) >= self.MIN_POINTS_EXTRAPOL):
            conso_fenetre = Decimal(str(row2[0])) - Decimal(str(row2[1]))
            span_days     = max(1, int(row2[3]))
            if conso_fenetre > Decimal("0"):
                moy_jour = conso_fenetre / Decimal(str(span_days))
                extrapol = moy_jour * Decimal(str(nb_jours_periode))
                return extrapol, int(row2[2])

        return None, 0

    # ── get_conso_periode (Grid) — inchangé ───────────────────────────────────

    def get_conso_periode(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], str]:
        conn = None
        SEUIL = Decimal("0.1")
        nb_jours_periode = (date_fin - date_debut).days + 1

        try:
            conn = self._open_connection()
            col  = self._col(conn)

            sql_exact = f"""
                SELECT
                    SUM({col})    AS total,
                    COUNT([Date]) AS nb_points
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] >= ?
                  AND [Date] <= ?
                  AND {col} IS NOT NULL
                  AND {col} > ?
            """
            cursor = conn.cursor()
            cursor.execute(sql_exact, (site_id, date_debut, date_fin, SEUIL))
            row = cursor.fetchone()

            if row and row[0] is not None and row[1] and int(row[1]) >= self.MIN_DAYS_DENSE:
                return Decimal(str(row[0])), "exact"

            if row and row[0] is not None and row[1] and int(row[1]) >= self.MIN_POINTS_EXTRAPOL:
                moy_jour = Decimal(str(row[0])) / Decimal(str(row[1]))
                extrapol = moy_jour * Decimal(str(nb_jours_periode))
                return extrapol, "extrapol"

            fenetre_debut = date_debut - timedelta(days=90)
            fenetre_fin   = date_fin   + timedelta(days=90)

            sql_fenetre = f"""
                SELECT
                    AVG({col})    AS moy_jour,
                    COUNT([Date]) AS nb_points
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] >= ?
                  AND [Date] <= ?
                  AND {col} IS NOT NULL
                  AND {col} > ?
            """
            cursor.execute(sql_fenetre, (site_id, fenetre_debut, fenetre_fin, SEUIL))
            row2 = cursor.fetchone()

            if row2 and row2[0] is not None and row2[1] and int(row2[1]) >= self.MIN_POINTS_EXTRAPOL:
                moy_jour = Decimal(str(row2[0]))
                extrapol = moy_jour * Decimal(str(nb_jours_periode))
                return extrapol, "extrapol"

            return None, "none"

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_periode: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ── get_conso_last_complete_month (Grid) — inchangé ───────────────────────

    def get_conso_last_complete_month(
        self,
        site_id: str,
        before_date: date,
        min_days: int = 20,
    ) -> tuple[Optional[Decimal], Optional[date]]:
        conn = None
        SEUIL = Decimal("0.1")

        try:
            conn  = self._open_connection()
            col   = self._col(conn)

            sql_dense = f"""
                SELECT TOP 1
                    YEAR([Date])  AS yr,
                    MONTH([Date]) AS mo,
                    SUM({col})    AS conso,
                    COUNT([Date]) AS nb_jours
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] < ?
                  AND {col} IS NOT NULL
                  AND {col} > ?
                GROUP BY YEAR([Date]), MONTH([Date])
                HAVING COUNT([Date]) >= ?
                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
            """
            cursor = conn.cursor()
            cursor.execute(sql_dense, (site_id, before_date, SEUIL, min_days))
            row = cursor.fetchone()

            if row and row[2] is not None:
                ref_date = date(int(row[0]), int(row[1]), 1)
                return Decimal(str(row[2])), ref_date

            sql_partiel = f"""
                SELECT TOP 1
                    YEAR([Date])   AS yr,
                    MONTH([Date])  AS mo,
                    AVG({col})     AS moy_jour,
                    COUNT([Date])  AS nb_jours
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] < ?
                  AND {col} IS NOT NULL
                  AND {col} > ?
                GROUP BY YEAR([Date]), MONTH([Date])
                HAVING COUNT([Date]) >= ?
                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
            """
            cursor.execute(sql_partiel, (site_id, before_date, SEUIL, self.MIN_POINTS_EXTRAPOL))
            row2 = cursor.fetchone()

            if row2 and row2[2] is not None:
                ref_date = date(int(row2[0]), int(row2[1]), 1)
                extrapol_30j = Decimal(str(row2[2])) * Decimal("30")
                return extrapol_30j, ref_date

            return None, None

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_last_complete_month: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ── ✅ NOUVEAU — get_conso_acm ─────────────────────────────────────────────

    def get_conso_acm(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """
        Récupère la consommation ACM (AC Meter) depuis act_energy_p.

        act_energy_p  = énergie active mesurée par le compteur triphasé ACT [kWh/jour]
        act2_energy_p = compteur secondaire (fallback si act_energy_p insuffisant)

        Applique la même logique adaptive que get_conso_periode (Grid) :
          - dense  (≥20 pts)  → SUM exact
          - épars  (≥3 pts)   → moyenne × nb_jours_période
          - ±90j   (≥3 pts)   → fenêtre élargie

        Retourne
        --------
        (conso_acm_periode_kwh, conso_acm_30j_kwh)
            Les deux peuvent être None si données insuffisantes.
        """
        conn = None
        try:
            conn   = self._open_connection()
            cursor = conn.cursor()

            # ── Période ───────────────────────────────────────────────────────
            conso_p, nb_p = self._query_acm_energy(
                cursor, site_id, date_debut, date_fin,
                col=self.COL_ACM_ENERGY_PRIMARY,
            )

            # Fallback act2_energy_p si act_energy_p insuffisant
            if conso_p is None:
                conso_p, nb_p = self._query_acm_energy(
                    cursor, site_id, date_debut, date_fin,
                    col=self.COL_ACM_ENERGY_SECONDARY,
                )
                if conso_p is not None:
                    logger.info(
                        "[eFMS ACM] %s : fallback act2_energy_p → %.2f kWh (%d pts)",
                        site_id, float(conso_p), nb_p,
                    )

            # ── 30 derniers jours de la période ──────────────────────────────
            d30_fin   = date_fin
            d30_debut = d30_fin - timedelta(days=29)

            conso_30j, nb_30j = self._query_acm_energy(
                cursor, site_id, d30_debut, d30_fin,
                col=self.COL_ACM_ENERGY_PRIMARY,
            )
            if conso_30j is None:
                conso_30j, nb_30j = self._query_acm_energy(
                    cursor, site_id, d30_debut, d30_fin,
                    col=self.COL_ACM_ENERGY_SECONDARY,
                )

            if conso_p is not None or conso_30j is not None:
                logger.info(
                    "[eFMS ACM] %s %s→%s : période=%.2f kWh (%d pts), 30j=%.2f kWh (%d pts)",
                    site_id, date_debut, date_fin,
                    float(conso_p or 0), nb_p,
                    float(conso_30j or 0), nb_30j,
                )
            else:
                logger.info("[eFMS ACM] %s : aucune donnée ACM disponible", site_id)

            return conso_p, conso_30j

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_acm: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ── check_connection & diagnose (inchangés) ────────────────────────────────

    def check_connection(self) -> bool:
        conn = None
        try:
            conn = self._open_connection()
            conn.cursor().execute("SELECT 1")
            return True
        except Exception:
            return False
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def diagnose(self) -> dict:
        import socket
        result = {
            "host": self.host, "port": self.port, "db": self.db,
            "driver": self.driver, "user": self.user,
            "tcp_reachable": False, "odbc_connected": False,
            "query_ok": False, "row_count_test": None,
            "table_columns": None, "col_conso_resolved": None,
            "sample_5_rows": None, "mode_guess": None, "error": None,
        }
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5)
            sock.close()
            result["tcp_reachable"] = True
        except Exception as e:
            result["error"] = f"TCP: {e}"
            return result

        conn = None
        try:
            conn = pyodbc.connect(self._build_conn_string(), timeout=self.timeout)
            result["odbc_connected"] = True
            conn.cursor().execute("SELECT 1")
            result["query_ok"] = True
        except Exception as e:
            result["error"] = f"ODBC: {e}"
            if conn:
                conn.close()
            return result

        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
            result["table_columns"] = [d[0] for d in cursor.description]
            col = self._resolve_col_conso(conn)
            result["col_conso_resolved"] = col
            cursor.execute(f"""
                SELECT TOP 5 site_id, [Date], [{col}]
                FROM {self.TABLE_GRID}
                WHERE [{col}] IS NOT NULL AND [{col}] > 0
                ORDER BY site_id, [Date]
            """)
            rows = cursor.fetchall()
            result["sample_5_rows"] = [
                {"site_id": r[0], "date": str(r[1]), "conso": float(r[2])}
                for r in rows
            ]
            result["row_count_test"] = len(rows)
            if rows:
                avg = sum(float(r[2]) for r in rows) / len(rows)
                result["mode_guess"] = "delta" if avg > 5_000 else "sum"
        except Exception as e:
            result["error"] = f"Table check: {e}"
        finally:
            try: conn.close()
            except Exception: pass

        return result