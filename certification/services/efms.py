# certification/services/efms.py
# v4 — ACM en source primaire, Grid en fallback
#       prefetch_batch() : 2 requêtes SQL pour tout le batch (au lieu de N×2)

import time
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

# pyodbc importé localement (voir _open_connection/diagnose), pas ici : ce
# module doit rester importable même sans le driver ODBC système
# (unixodbc/msodbcsql17) installé — EfmsService n'est plus la voie
# d'accès aux données en production (SnowflakeEfmsService en hérite
# seulement pour réutiliser _delta_to_conso/_sum_to_conso, du pur calcul,
# jamais pyodbc), seuls les diagnostics eFMS manuels en ont encore besoin.
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class EfmsConnectionError(Exception):
    pass

class EfmsQueryError(Exception):
    pass


class EfmsService:

    TABLE_GRID = "[SQL1-ProdDB].[dbo].[silver.gfms_Grid_Report_day]"
    TABLE_ACM  = "[SQL1-ProdDB].[dbo].[silver.gfms_AC_Meter_Report_day]"

    _COL_CONSO_CANDIDATES = [
        "GRID_ENERGY_CONSO_PER_DAY",   # ← table test (nvarchar)
        "Grid Energy Conso per Day",
        "Grid_Energy_Conso_per_day",
        "Grid_Energy_Conso_per_Day",
        "GridEnergyConso",
        "energy_kwh",
        "conso_kwh",
    ]
    _col_conso_resolved: Optional[str] = None

    MIN_DAYS_DENSE      = 20
    MIN_POINTS_EXTRAPOL = 3

    # act_energy_p = INDEX CUMULATIF kWh — conso = MAX - MIN sur la période
    COL_ACM_ENERGY_PRIMARY   = "act_energy_p"
    COL_ACM_ENERGY_SECONDARY = "act2_energy_p"

    # TTL cache Redis (2h — largement suffisant pour un batch)
    CACHE_TTL = 7200

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

    # ─────────────────────────────────────────────────────────────────────────
    # Connexion
    # ─────────────────────────────────────────────────────────────────────────

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
        import pyodbc
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
                error_msg   = str(e)
                last_error  = e
                log_status  = (EfmsConnectionLog.Status.TIMEOUT
                               if "timeout" in error_msg.lower()
                               else EfmsConnectionLog.Status.VPN_DOWN)
                logger.warning(f"[eFMS] Tentative {attempt}/{self.max_retries}: {error_msg[:200]}")
                EfmsConnectionLog.objects.create(
                    cert_batch=self.cert_batch, status=log_status,
                    duration_ms=duration_ms, host=self.host, error=error_msg[:500],
                )
            except pyodbc.Error as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                error_msg   = str(e)
                last_error  = e
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

    def _resolve_col_conso(self, conn) -> str:
        if EfmsService._col_conso_resolved:
            return EfmsService._col_conso_resolved

        cursor = conn.cursor()
        cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
        columns = [d[0] for d in cursor.description]

        columns_lower = {c.lower(): c for c in columns}
        for candidate in self._COL_CONSO_CANDIDATES:
            match = columns_lower.get(candidate.lower())
            if match:
                EfmsService._col_conso_resolved = match
                logger.info(f"[eFMS] Colonne Grid résolue → [{match}]")
                return match

        for col in columns:
            if any(k in col.lower() for k in ("conso", "energy", "kwh")):
                EfmsService._col_conso_resolved = col
                return col

        raise EfmsQueryError(f"Colonne énergie Grid introuvable. Colonnes: {columns}")

    # ─────────────────────────────────────────────────────────────────────────
    # Clés de cache Redis
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key_acm(cert_batch_id: int) -> str:
        return f"efms:acm:batch:{cert_batch_id}"

    @staticmethod
    def _cache_key_grid(cert_batch_id: int) -> str:
        return f"efms:grid:batch:{cert_batch_id}"

    @staticmethod
    def _cache_entry_key(site_id: str, date_debut: date, date_fin: date) -> str:
        """
        Clé composite site+période dans le cache d'un batch. Un même site peut
        avoir plusieurs factures (donc plusieurs périodes) dans un même batch
        de certification — indexer uniquement par site_id ferait que la
        dernière période traitée écrase silencieusement le cache des autres
        factures de ce site.
        """
        return f"{site_id}|{date_debut.isoformat()}|{date_fin.isoformat()}"

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers conversion → conso kWh
    # ─────────────────────────────────────────────────────────────────────────

    def _delta_to_conso(
        self,
        idx_max,
        idx_min,
        nb_pts: int,
        span_days: int,
        nb_jours_cible: int,
        fenetre_delta=None,
        fenetre_nb: int = 0,
        fenetre_span: int = 1,
    ) -> Optional[Decimal]:
        """
        Convertit MAX-MIN d'un index cumulatif ACM en conso kWh.
        Logique adaptive : exact si dense, extrapolation sinon, ±90j en dernier recours.
        """
        if idx_max is not None and idx_min is not None and nb_pts >= self.MIN_POINTS_EXTRAPOL:
            delta = Decimal(str(idx_max)) - Decimal(str(idx_min))
            if delta > Decimal("0"):
                if nb_pts >= self.MIN_DAYS_DENSE:
                    return delta
                span = max(1, span_days)
                moy  = delta / Decimal(str(span))
                return moy * Decimal(str(nb_jours_cible))

        # Fenêtre ±90j
        if fenetre_delta is not None and fenetre_nb >= self.MIN_POINTS_EXTRAPOL:
            d = Decimal(str(fenetre_delta))
            if d > Decimal("0"):
                moy = d / Decimal(str(max(1, fenetre_span)))
                return moy * Decimal(str(nb_jours_cible))

        return None

    def _sum_to_conso(
        self,
        total,
        nb_pts: int,
        nb_jours_cible: int,
        fenetre_moy=None,
        fenetre_nb: int = 0,
    ) -> Optional[Decimal]:
        """Convertit un SUM Grid en conso kWh, avec extrapolation si épars."""
        if total is not None and nb_pts >= self.MIN_POINTS_EXTRAPOL:
            t = Decimal(str(total))
            if nb_pts >= self.MIN_DAYS_DENSE:
                return t
            moy = t / Decimal(str(nb_pts))
            return moy * Decimal(str(nb_jours_cible))

        if fenetre_moy is not None and fenetre_nb >= self.MIN_POINTS_EXTRAPOL:
            return Decimal(str(fenetre_moy)) * Decimal(str(nb_jours_cible))

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # PREFETCH BULK — cœur de l'optimisation
    # ─────────────────────────────────────────────────────────────────────────

    def prefetch_batch(
        self,
        cert_batch_id: int,
        site_ids: list[str],
        date_debut: date,
        date_fin: date,
    ) -> None:
        """
        Charge TOUTES les données ACM + Grid pour tous les sites du batch
        en quelques requêtes SQL bulk, puis stocke en cache Redis.

        Appelé UNE FOIS dans launch_certification_batch avant le fan-out Celery.
        Réduit N×2 connexions SQL → ~2×(nb_chunks) connexions pour tout le batch.

        SQL Server limite les paramètres à ~2100 par requête.
        → Chunking automatique par tranches de CHUNK_SIZE sites.

        Structure du cache ACM  {site_id: {"periode": Decimal|None, "30j": Decimal|None}}
        Structure du cache Grid {site_id: {"periode": Decimal|None, "30j": Decimal|None,
                                           "last_month_date": date|None}}
        """
        if not site_ids:
            return

        CHUNK_SIZE = 400  # Sécurité : 400 sites × 1 param = bien en dessous de 2100

        nb_jours_periode = (date_fin - date_debut).days + 1
        d30_debut        = date_fin - timedelta(days=29)
        fenetre_debut    = date_debut - timedelta(days=90)
        fenetre_fin      = date_fin   + timedelta(days=90)

        # Résultat agrégé sur tous les chunks
        acm_cache:  dict[str, dict] = {}
        grid_cache: dict[str, dict] = {}

        conn = None
        try:
            conn   = self._open_connection()
            cursor = conn.cursor()
            col    = self._resolve_col_conso(conn)

            logger.info(
                "[eFMS prefetch] %d sites → %d chunks de %d — %s→%s",
                len(site_ids), (len(site_ids) + CHUNK_SIZE - 1) // CHUNK_SIZE,
                CHUNK_SIZE, date_debut, date_fin,
            )

            for chunk_start in range(0, len(site_ids), CHUNK_SIZE):
                chunk = site_ids[chunk_start: chunk_start + CHUNK_SIZE]
                ph    = ",".join("?" * len(chunk))

                # ══════════════════════════════════════════════════════════════
                # BLOC ACM — act_energy_p (index cumulatif, conso = MAX - MIN)
                # ══════════════════════════════════════════════════════════════

                def _acm_query(d_s, d_e):
                    cursor.execute(f"""
                        SELECT
                            site_id,
                            MAX(act_energy_p)  AS idx_max,
                            MIN(act_energy_p)  AS idx_min,
                            COUNT([Date])      AS nb_pts,
                            DATEDIFF(day, MIN([Date]), MAX([Date])) AS span_days
                        FROM {self.TABLE_ACM}
                        WHERE site_id IN ({ph})
                          AND [Date] >= ? AND [Date] <= ?
                          AND act_energy_p IS NOT NULL AND act_energy_p > 0
                        GROUP BY site_id
                    """, (*chunk, d_s, d_e))
                    return {r[0]: r for r in cursor.fetchall()}

                def _acm_fenetre_query():
                    cursor.execute(f"""
                        SELECT
                            site_id,
                            MAX(act_energy_p) - MIN(act_energy_p) AS delta,
                            COUNT([Date])                          AS nb_pts,
                            DATEDIFF(day, MIN([Date]), MAX([Date])) AS span_days
                        FROM {self.TABLE_ACM}
                        WHERE site_id IN ({ph})
                          AND [Date] >= ? AND [Date] <= ?
                          AND act_energy_p IS NOT NULL AND act_energy_p > 0
                        GROUP BY site_id
                    """, (*chunk, fenetre_debut, fenetre_fin))
                    return {r[0]: r for r in cursor.fetchall()}

                acm_p_rows  = _acm_query(date_debut, date_fin)
                acm_30_rows = _acm_query(d30_debut,  date_fin)
                acm_f_rows  = _acm_fenetre_query()

                for sid in chunk:
                    rp = acm_p_rows.get(sid)
                    r3 = acm_30_rows.get(sid)
                    rf = acm_f_rows.get(sid)

                    def _d(r, nb_j, _rf=rf):
                        if r is None:
                            return self._delta_to_conso(
                                None, None, 0, 1, nb_j,
                                _rf[1] if _rf else None,
                                int(_rf[2]) if _rf else 0,
                                int(_rf[3]) if _rf else 1,
                            )
                        return self._delta_to_conso(
                            r[1], r[2], int(r[3]), int(r[4]), nb_j,
                            _rf[1] if _rf else None,
                            int(_rf[2]) if _rf else 0,
                            int(_rf[3]) if _rf else 1,
                        )

                    acm_cache[sid] = {"periode": _d(rp, nb_jours_periode), "30j": _d(r3, 30)}

                # ══════════════════════════════════════════════════════════════
                # BLOC GRID
                # ══════════════════════════════════════════════════════════════

                def _grid_query(d_s, d_e):
                    cursor.execute(f"""
                        SELECT
                            site_id,
                            SUM(TRY_CAST([{col}] AS FLOAT))   AS total,
                            COUNT([Date])                      AS nb_pts
                        FROM {self.TABLE_GRID}
                        WHERE site_id IN ({ph})
                        AND [Date] >= ? AND [Date] <= ?
                        AND [{col}] IS NOT NULL
                        AND TRY_CAST([{col}] AS FLOAT) > 0.1
                        GROUP BY site_id
                    """, (*chunk, d_s, d_e))
                    return {r[0]: r for r in cursor.fetchall()}

                grid_p_rows  = _grid_query(date_debut, date_fin)
                grid_30_rows = _grid_query(d30_debut,  date_fin)

                cursor.execute(f"""
                    SELECT site_id,
                        AVG(TRY_CAST([{col}] AS FLOAT)),
                        COUNT([Date])
                    FROM {self.TABLE_GRID}
                    WHERE site_id IN ({ph})
                    AND [Date] >= ? AND [Date] <= ?
                    AND [{col}] IS NOT NULL
                    AND TRY_CAST([{col}] AS FLOAT) > 0.1
                    GROUP BY site_id
                """, (*chunk, fenetre_debut, fenetre_fin))
                grid_f_rows = {r[0]: r for r in cursor.fetchall()}

                cursor.execute(f"""
                    SELECT t.site_id, t.yr, t.mo, t.conso, t.nb_jours
                    FROM (
                        SELECT
                            site_id,
                            YEAR([Date])                        AS yr,
                            MONTH([Date])                       AS mo,
                            SUM(TRY_CAST([{col}] AS FLOAT))    AS conso,
                            COUNT([Date])                       AS nb_jours,
                            ROW_NUMBER() OVER (
                                PARTITION BY site_id
                                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
                            ) AS rn
                        FROM {self.TABLE_GRID}
                        WHERE site_id IN ({ph})
                        AND [Date] < ?
                        AND [{col}] IS NOT NULL
                        AND TRY_CAST([{col}] AS FLOAT) > 0.1
                        GROUP BY site_id, YEAR([Date]), MONTH([Date])
                        HAVING COUNT([Date]) >= {self.MIN_POINTS_EXTRAPOL}
                    ) t WHERE t.rn = 1
                """, (*chunk, date_debut))
                grid_lm_rows = {r[0]: r for r in cursor.fetchall()}

                for sid in chunk:
                    gp  = grid_p_rows.get(sid)
                    g30 = grid_30_rows.get(sid)
                    gf  = grid_f_rows.get(sid)
                    glm = grid_lm_rows.get(sid)

                    def _g(r, nb_j, _gf=gf):
                        return self._sum_to_conso(
                            r[1] if r else None,
                            int(r[2]) if r else 0,
                            nb_j,
                            _gf[1] if _gf else None,
                            int(_gf[2]) if _gf else 0,
                        )

                    conso_30j       = _g(g30, 30)
                    last_month_date = None

                    if conso_30j is None and glm is not None and glm[3] is not None:
                        nb_j_lm = int(glm[4])
                        if nb_j_lm >= self.MIN_DAYS_DENSE:
                            conso_30j = Decimal(str(glm[3]))
                        elif nb_j_lm >= self.MIN_POINTS_EXTRAPOL:
                            conso_30j = Decimal(str(glm[3])) / Decimal(str(nb_j_lm)) * 30
                        last_month_date = date(int(glm[1]), int(glm[2]), 1)

                    grid_cache[sid] = {
                        "periode":         _g(gp, nb_jours_periode),
                        "30j":             conso_30j,
                        "last_month_date": last_month_date,
                    }

            # ── Stockage final en cache ───────────────────────────────────────
            cache.set(self._cache_key_acm(cert_batch_id),  acm_cache,  timeout=self.CACHE_TTL)
            cache.set(self._cache_key_grid(cert_batch_id), grid_cache, timeout=self.CACHE_TTL)

            nb_acm  = sum(1 for v in acm_cache.values()  if v["periode"] or v["30j"])
            nb_grid = sum(1 for v in grid_cache.values() if v["periode"] or v["30j"])
            logger.info(
                "[eFMS prefetch] Terminé — ACM %d/%d sites · Grid %d/%d sites",
                nb_acm, len(site_ids), nb_grid, len(site_ids),
            )

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"prefetch_batch: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ─────────────────────────────────────────────────────────────────────────
    # Lecture depuis le cache (utilisé par certify_single_invoice)
    # ─────────────────────────────────────────────────────────────────────────

    def get_conso_acm_cached(
        self, cert_batch_id: int, site_id: str, date_debut: date, date_fin: date
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Retourne (conso_periode, conso_30j) ACM depuis le cache (clé site+période)."""
        data = cache.get(self._cache_key_acm(cert_batch_id)) or {}
        entry = data.get(self._cache_entry_key(site_id, date_debut, date_fin), {})
        return entry.get("periode"), entry.get("30j")

    def get_conso_grid_cached(
        self, cert_batch_id: int, site_id: str, date_debut: date, date_fin: date
    ) -> tuple[Optional[Decimal], Optional[Decimal], Optional[date]]:
        """Retourne (conso_periode, conso_30j, last_month_date) Grid depuis le cache (clé site+période)."""
        data = cache.get(self._cache_key_grid(cert_batch_id)) or {}
        entry = data.get(self._cache_entry_key(site_id, date_debut, date_fin), {})
        return entry.get("periode"), entry.get("30j"), entry.get("last_month_date")

    # ─────────────────────────────────────────────────────────────────────────
    # Fallbacks ponctuels (si cache absent — ne devrait pas arriver en prod)
    # ─────────────────────────────────────────────────────────────────────────

    def get_conso_acm(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        """Requête ACM ponctuelle (fallback si cache manquant)."""
        conn = None
        try:
            conn   = self._open_connection()
            cursor = conn.cursor()
            nb_jours = (date_fin - date_debut).days + 1
            d30_debut = date_fin - timedelta(days=29)

            fenetre_debut = date_debut - timedelta(days=90)
            fenetre_fin   = date_fin   + timedelta(days=90)

            def _q(d_s, d_e, nb_j):
                cursor.execute(f"""
                    SELECT MAX(act_energy_p), MIN(act_energy_p),
                           COUNT([Date]),
                           DATEDIFF(day, MIN([Date]), MAX([Date]))
                    FROM {self.TABLE_ACM}
                    WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                      AND act_energy_p IS NOT NULL AND act_energy_p > 0
                """, (site_id, d_s, d_e))
                r = cursor.fetchone()

                cursor.execute(f"""
                    SELECT MAX(act_energy_p) - MIN(act_energy_p),
                           COUNT([Date]),
                           DATEDIFF(day, MIN([Date]), MAX([Date]))
                    FROM {self.TABLE_ACM}
                    WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                      AND act_energy_p IS NOT NULL AND act_energy_p > 0
                """, (site_id, fenetre_debut, fenetre_fin))
                rf = cursor.fetchone()

                return self._delta_to_conso(
                    r[0], r[1], int(r[2] or 0), int(r[3] or 0), nb_j,
                    rf[0] if rf else None,
                    int(rf[1] or 0) if rf else 0,
                    int(rf[2] or 1) if rf else 1,
                )

            return _q(date_debut, date_fin, nb_jours), _q(d30_debut, date_fin, 30)

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_acm: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def get_conso_periode(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], str]:
        """Requête Grid ponctuelle (fallback si cache manquant)."""
        conn = None
        SEUIL = Decimal("0.1")
        nb_jours_periode = (date_fin - date_debut).days + 1
        try:
            conn   = self._open_connection()
            col    = self._resolve_col_conso(conn)
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT SUM(TRY_CAST([{col}] AS FLOAT)), COUNT([Date])
                FROM {self.TABLE_GRID}
                WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                AND [{col}] IS NOT NULL
                AND TRY_CAST([{col}] AS FLOAT) > ?
            """, (site_id, date_debut, date_fin, float(SEUIL)))
            row = cursor.fetchone() 
            
            if row and row[0] is not None and row[1]:
                nb = int(row[1])
                if nb >= self.MIN_DAYS_DENSE:
                    return Decimal(str(row[0])), "exact"
                if nb >= self.MIN_POINTS_EXTRAPOL:
                    return Decimal(str(row[0])) / Decimal(str(nb)) * Decimal(str(nb_jours_periode)), "extrapol"

            fenetre_debut = date_debut - timedelta(days=90)
            fenetre_fin   = date_fin   + timedelta(days=90)
            cursor.execute(f"""
                SELECT AVG([{col}]), COUNT([Date])
                FROM {self.TABLE_GRID}
                WHERE site_id = ? AND [Date] >= ? AND [Date] <= ?
                  AND [{col}] IS NOT NULL AND [{col}] > ?
            """, (site_id, fenetre_debut, fenetre_fin, SEUIL))
            row2 = cursor.fetchone()
            if row2 and row2[0] and int(row2[1] or 0) >= self.MIN_POINTS_EXTRAPOL:
                return Decimal(str(row2[0])) * Decimal(str(nb_jours_periode)), "extrapol"

            return None, "none"
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_periode: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def get_conso_last_complete_month(
        self,
        site_id: str,
        before_date: date,
        min_days: int = 20,
    ) -> tuple[Optional[Decimal], Optional[date]]:
        """Requête Grid dernier mois complet ponctuelle (fallback si cache manquant)."""
        conn = None
        SEUIL = Decimal("0.1")
        try:
            conn   = self._open_connection()
            col    = self._resolve_col_conso(conn)
            cursor = conn.cursor()

            cursor.execute(f"""
                SELECT TOP 1 YEAR([Date]), MONTH([Date]),
                    AVG(TRY_CAST([{col}] AS FLOAT)), COUNT([Date])
                FROM {self.TABLE_GRID}
                WHERE site_id = ? AND [Date] < ?
                AND [{col}] IS NOT NULL
                AND TRY_CAST([{col}] AS FLOAT) > ?
                GROUP BY YEAR([Date]), MONTH([Date])
                HAVING COUNT([Date]) >= ?
                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
            """, (site_id, before_date, float(SEUIL), self.MIN_POINTS_EXTRAPOL))
            row = cursor.fetchone()
            if row and row[2]:
                return Decimal(str(row[2])), date(int(row[0]), int(row[1]), 1)

            cursor.execute(f"""
                SELECT TOP 1 YEAR([Date]), MONTH([Date]), AVG([{col}]), COUNT([Date])
                FROM {self.TABLE_GRID}
                WHERE site_id = ? AND [Date] < ? AND [{col}] IS NOT NULL AND [{col}] > ?
                GROUP BY YEAR([Date]), MONTH([Date])
                HAVING COUNT([Date]) >= ?
                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
            """, (site_id, before_date, SEUIL, self.MIN_POINTS_EXTRAPOL))
            row2 = cursor.fetchone()
            if row2 and row2[2]:
                return Decimal(str(row2[2])) * Decimal("30"), date(int(row2[0]), int(row2[1]), 1)
            return None, None
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_last_complete_month: {e}") from e
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    # ─────────────────────────────────────────────────────────────────────────
    # check_connection & diagnose
    # ─────────────────────────────────────────────────────────────────────────

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
        import pyodbc
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
            if conn: conn.close()
            return result

        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
            result["table_columns"] = [d[0] for d in cursor.description]
            col = self._resolve_col_conso(conn)
            result["col_conso_resolved"] = col
            cursor.execute(f"""
                SELECT TOP 5 site_id, [Date], TRY_CAST([{col}] AS FLOAT)
                FROM {self.TABLE_GRID}
                WHERE [{col}] IS NOT NULL
                AND TRY_CAST([{col}] AS FLOAT) > 0
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