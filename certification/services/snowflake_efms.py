# certification/services/snowflake_efms.py
"""
Service miroir d'EfmsService — même interface publique, même logique
d'extrapolation (héritée, source unique de vérité), mais lit les données
Grid/ACM depuis Snowflake (DB_GFMS_ANALYTICS_PROD.GOLD.GRID_REPORT / AC_METER)
au lieu de SQL Server. SITE_ID direct, pas de mapping data_id nécessaire.

Objectif : tourner en parallèle d'EfmsService le temps de valider les
résultats sur des factures déjà certifiées, avant tout cutover réel.
Clés de cache Redis préfixées différemment pour ne pas entrer en collision
avec EfmsService pendant la phase de comparaison.
"""
import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from django.core.cache import cache

from certification.services.efms import EfmsService, EfmsConnectionError, EfmsQueryError
from certification.services.snowflake_service import SnowflakeService, SnowflakeConnectionError

logger = logging.getLogger(__name__)


class SnowflakeEfmsService(EfmsService):
    """
    Hérite d'EfmsService uniquement pour réutiliser _delta_to_conso/_sum_to_conso
    (logique pure, indépendante de la source SQL). Toutes les méthodes qui
    exécutent des requêtes sont réécrites pour Snowflake.
    """

    def __init__(self, cert_batch=None):
        self.cert_batch = cert_batch
        self.sf = SnowflakeService()

    # ─────────────────────────────────────────────────────────────────────────
    # Clés de cache — préfixées "sf:" pour coexister avec EfmsService pendant
    # la phase de comparaison (pas de collision de cache Redis).
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _cache_key_acm(cert_batch_id: int) -> str:
        return f"sf:efms:acm:batch:{cert_batch_id}"

    @staticmethod
    def _cache_key_grid(cert_batch_id: int) -> str:
        return f"sf:efms:grid:batch:{cert_batch_id}"

    def check_connection(self) -> bool:
        return self.sf.check_connection()

    # ─────────────────────────────────────────────────────────────────────────
    # PREFETCH BULK
    # ─────────────────────────────────────────────────────────────────────────

    def prefetch_batch(
        self,
        cert_batch_id: int,
        groups: list[tuple[list[str], date, date]],
    ) -> None:
        """
        `groups` : liste de (site_ids, date_debut, date_fin) — un groupe par
        période de facture DISTINCTE dans le batch, pas une seule fenêtre
        min(debuts)/max(fins) pour tout le batch. Sans ce regroupement par
        période réelle, un site facturé sur ~30 jours mais batché avec des
        sites d'autres périodes se voyait attribuer une "conso période"
        calculée sur l'enveloppe globale du batch (parfois plusieurs mois),
        au lieu de sa propre période — écarts constatés jusqu'à ×6 en prod.
        Les sites qui partagent exactement la même période restent groupés
        dans une seule requête bulk (l'optimisation de coût Snowflake est
        préservée), seule l'hétérogénéité entre groupes est corrigée.
        """
        if not groups:
            return

        acm_cache: dict[str, dict] = {}
        grid_cache: dict[str, dict] = {}
        total_sites = sum(len(site_ids) for site_ids, _, _ in groups)

        try:
            conn = self.sf._connect()
        except SnowflakeConnectionError as e:
            raise EfmsConnectionError(f"Connexion Snowflake échouée: {e}") from e

        try:
            cursor = conn.cursor()
            db_schema = f"{self.sf.database}.{self.sf.schema}"

            logger.info(
                "[Snowflake-eFMS prefetch] %d sites, %d période(s) distincte(s)",
                total_sites, len(groups),
            )

            for site_ids, date_debut, date_fin in groups:
                if not site_ids:
                    continue

                nb_jours_periode = (date_fin - date_debut).days + 1
                d30_debut = date_fin - timedelta(days=29)
                fenetre_debut = date_debut - timedelta(days=90)
                fenetre_fin = date_fin + timedelta(days=90)

                for chunk in self.sf._chunks(site_ids):
                    ph = ",".join(["%s"] * len(chunk))

                    # COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) : ~39 sites (sur ~7900) ne
                    # remontent jamais ACT_ENERGY_P et ne reportent que sous ACM_ENERGY_P
                    # (vérifié en base) — sans ce repli, ces sites n'ont jamais de conso ACM.
                    def _acm_query(d_s, d_e):
                        cursor.execute(f"""
                            SELECT
                                SITE_ID,
                                MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P))  AS idx_max,
                                MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P))  AS idx_min,
                                COUNT(DATE)        AS nb_pts,
                                DATEDIFF('day', MIN(DATE), MAX(DATE)) AS span_days
                            FROM {db_schema}.AC_METER
                            WHERE SITE_ID IN ({ph})
                              AND DATE >= %s AND DATE <= %s
                              AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL
                              AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
                            GROUP BY SITE_ID
                        """, tuple(chunk) + (d_s, d_e))
                        return {r[0]: r for r in cursor.fetchall()}

                    def _acm_fenetre_query():
                        cursor.execute(f"""
                            SELECT
                                SITE_ID,
                                MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) - MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) AS delta,
                                COUNT(DATE)                            AS nb_pts,
                                DATEDIFF('day', MIN(DATE), MAX(DATE))  AS span_days
                            FROM {db_schema}.AC_METER
                            WHERE SITE_ID IN ({ph})
                              AND DATE >= %s AND DATE <= %s
                              AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL
                              AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
                            GROUP BY SITE_ID
                        """, tuple(chunk) + (fenetre_debut, fenetre_fin))
                        return {r[0]: r for r in cursor.fetchall()}

                    acm_p_rows = _acm_query(date_debut, date_fin)
                    acm_30_rows = _acm_query(d30_debut, date_fin)
                    acm_f_rows = _acm_fenetre_query()

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

                        # Clé composite site+période : un même site peut avoir plusieurs
                        # factures (donc plusieurs périodes) dans un même batch — une clé
                        # par seul site_id écraserait silencieusement les autres factures
                        # de ce site avec les valeurs de la dernière période traitée.
                        acm_cache[self._cache_entry_key(sid, date_debut, date_fin)] = {
                            "periode": _d(rp, nb_jours_periode), "30j": _d(r3, 30),
                        }

                    def _grid_query(d_s, d_e):
                        cursor.execute(f"""
                            SELECT
                                SITE_ID,
                                SUM(GRID_ENERGY_CONSO_PER_DAY) AS total,
                                COUNT(DATE)                     AS nb_pts
                            FROM {db_schema}.GRID_REPORT
                            WHERE SITE_ID IN ({ph})
                              AND DATE >= %s AND DATE <= %s
                              AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                              AND GRID_ENERGY_CONSO_PER_DAY > 0.1
                            GROUP BY SITE_ID
                        """, tuple(chunk) + (d_s, d_e))
                        return {r[0]: r for r in cursor.fetchall()}

                    grid_p_rows = _grid_query(date_debut, date_fin)
                    grid_30_rows = _grid_query(d30_debut, date_fin)

                    cursor.execute(f"""
                        SELECT SITE_ID,
                            AVG(GRID_ENERGY_CONSO_PER_DAY),
                            COUNT(DATE)
                        FROM {db_schema}.GRID_REPORT
                        WHERE SITE_ID IN ({ph})
                          AND DATE >= %s AND DATE <= %s
                          AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                          AND GRID_ENERGY_CONSO_PER_DAY > 0.1
                        GROUP BY SITE_ID
                    """, tuple(chunk) + (fenetre_debut, fenetre_fin))
                    grid_f_rows = {r[0]: r for r in cursor.fetchall()}

                    cursor.execute(f"""
                        SELECT t.SITE_ID, t.yr, t.mo, t.conso, t.nb_jours
                        FROM (
                            SELECT
                                SITE_ID,
                                YEAR(DATE)                          AS yr,
                                MONTH(DATE)                         AS mo,
                                SUM(GRID_ENERGY_CONSO_PER_DAY)     AS conso,
                                COUNT(DATE)                         AS nb_jours,
                                ROW_NUMBER() OVER (
                                    PARTITION BY SITE_ID
                                    ORDER BY YEAR(DATE) DESC, MONTH(DATE) DESC
                                ) AS rn
                            FROM {db_schema}.GRID_REPORT
                            WHERE SITE_ID IN ({ph})
                              AND DATE < %s
                              AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                              AND GRID_ENERGY_CONSO_PER_DAY > 0.1
                            GROUP BY SITE_ID, YEAR(DATE), MONTH(DATE)
                            HAVING COUNT(DATE) >= {self.MIN_POINTS_EXTRAPOL}
                        ) t WHERE t.rn = 1
                    """, tuple(chunk) + (date_debut,))
                    grid_lm_rows = {r[0]: r for r in cursor.fetchall()}

                    for sid in chunk:
                        gp = grid_p_rows.get(sid)
                        g30 = grid_30_rows.get(sid)
                        gf = grid_f_rows.get(sid)
                        glm = grid_lm_rows.get(sid)

                        def _g(r, nb_j, _gf=gf):
                            return self._sum_to_conso(
                                r[1] if r else None,
                                int(r[2]) if r else 0,
                                nb_j,
                                _gf[1] if _gf else None,
                                int(_gf[2]) if _gf else 0,
                            )

                        conso_30j = _g(g30, 30)
                        last_month_date = None

                        if conso_30j is None and glm is not None and glm[3] is not None:
                            nb_j_lm = int(glm[4])
                            if nb_j_lm >= self.MIN_DAYS_DENSE:
                                conso_30j = Decimal(str(glm[3]))
                            elif nb_j_lm >= self.MIN_POINTS_EXTRAPOL:
                                conso_30j = Decimal(str(glm[3])) / Decimal(str(nb_j_lm)) * 30
                            last_month_date = date(int(glm[1]), int(glm[2]), 1)

                        grid_cache[self._cache_entry_key(sid, date_debut, date_fin)] = {
                            "periode": _g(gp, nb_jours_periode),
                            "30j": conso_30j,
                            "last_month_date": last_month_date,
                        }

            cache.set(self._cache_key_acm(cert_batch_id), acm_cache, timeout=self.CACHE_TTL)
            cache.set(self._cache_key_grid(cert_batch_id), grid_cache, timeout=self.CACHE_TTL)

            nb_acm = sum(1 for v in acm_cache.values() if v["periode"] or v["30j"])
            nb_grid = sum(1 for v in grid_cache.values() if v["periode"] or v["30j"])
            logger.info(
                "[Snowflake-eFMS prefetch] Terminé — ACM %d/%d sites · Grid %d/%d sites",
                nb_acm, total_sites, nb_grid, total_sites,
            )

        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"prefetch_batch (Snowflake): {e}") from e
        finally:
            try: conn.close()
            except Exception: pass

    # ─────────────────────────────────────────────────────────────────────────
    # Fallbacks ponctuels
    # ─────────────────────────────────────────────────────────────────────────

    def get_conso_acm(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], Optional[Decimal]]:
        try:
            conn = self.sf._connect()
        except SnowflakeConnectionError as e:
            raise EfmsConnectionError(f"Connexion Snowflake échouée: {e}") from e

        try:
            cursor = conn.cursor()
            db_schema = f"{self.sf.database}.{self.sf.schema}"
            nb_jours = (date_fin - date_debut).days + 1
            d30_debut = date_fin - timedelta(days=29)
            fenetre_debut = date_debut - timedelta(days=90)
            fenetre_fin   = date_fin   + timedelta(days=90)

            def _q(d_s, d_e, nb_j):
                cursor.execute(f"""
                    SELECT MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)), MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)),
                           COUNT(DATE),
                           DATEDIFF('day', MIN(DATE), MAX(DATE))
                    FROM {db_schema}.AC_METER
                    WHERE SITE_ID = %s AND DATE >= %s AND DATE <= %s
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
                """, (site_id, d_s, d_e))
                r = cursor.fetchone()

                cursor.execute(f"""
                    SELECT MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) - MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)),
                           COUNT(DATE),
                           DATEDIFF('day', MIN(DATE), MAX(DATE))
                    FROM {db_schema}.AC_METER
                    WHERE SITE_ID = %s AND DATE >= %s AND DATE <= %s
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
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
            raise EfmsQueryError(f"get_conso_acm (Snowflake): {e}") from e
        finally:
            try: conn.close()
            except Exception: pass

    def get_conso_periode(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> tuple[Optional[Decimal], str]:
        SEUIL = Decimal("0.1")
        nb_jours_periode = (date_fin - date_debut).days + 1
        try:
            conn = self.sf._connect()
        except SnowflakeConnectionError as e:
            raise EfmsConnectionError(f"Connexion Snowflake échouée: {e}") from e

        try:
            cursor = conn.cursor()
            db_schema = f"{self.sf.database}.{self.sf.schema}"

            cursor.execute(f"""
                SELECT SUM(GRID_ENERGY_CONSO_PER_DAY), COUNT(DATE)
                FROM {db_schema}.GRID_REPORT
                WHERE SITE_ID = %s AND DATE >= %s AND DATE <= %s
                  AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                  AND GRID_ENERGY_CONSO_PER_DAY > %s
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
                SELECT AVG(GRID_ENERGY_CONSO_PER_DAY), COUNT(DATE)
                FROM {db_schema}.GRID_REPORT
                WHERE SITE_ID = %s AND DATE >= %s AND DATE <= %s
                  AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL AND GRID_ENERGY_CONSO_PER_DAY > %s
            """, (site_id, fenetre_debut, fenetre_fin, float(SEUIL)))
            row2 = cursor.fetchone()
            if row2 and row2[0] and int(row2[1] or 0) >= self.MIN_POINTS_EXTRAPOL:
                return Decimal(str(row2[0])) * Decimal(str(nb_jours_periode)), "extrapol"

            return None, "none"
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_periode (Snowflake): {e}") from e
        finally:
            try: conn.close()
            except Exception: pass

    def get_conso_last_complete_month(
        self,
        site_id: str,
        before_date: date,
        min_days: int = 20,
    ) -> tuple[Optional[Decimal], Optional[date]]:
        SEUIL = Decimal("0.1")
        try:
            conn = self.sf._connect()
        except SnowflakeConnectionError as e:
            raise EfmsConnectionError(f"Connexion Snowflake échouée: {e}") from e

        try:
            cursor = conn.cursor()
            db_schema = f"{self.sf.database}.{self.sf.schema}"

            cursor.execute(f"""
                SELECT YEAR(DATE), MONTH(DATE),
                    AVG(GRID_ENERGY_CONSO_PER_DAY), COUNT(DATE)
                FROM {db_schema}.GRID_REPORT
                WHERE SITE_ID = %s AND DATE < %s
                  AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                  AND GRID_ENERGY_CONSO_PER_DAY > %s
                GROUP BY YEAR(DATE), MONTH(DATE)
                HAVING COUNT(DATE) >= %s
                ORDER BY YEAR(DATE) DESC, MONTH(DATE) DESC
                LIMIT 1
            """, (site_id, before_date, float(SEUIL), self.MIN_POINTS_EXTRAPOL))
            row = cursor.fetchone()
            if row and row[2]:
                return Decimal(str(row[2])), date(int(row[0]), int(row[1]), 1)
            return None, None
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_last_complete_month (Snowflake): {e}") from e
        finally:
            try: conn.close()
            except Exception: pass

    def diagnose(self) -> dict:
        result = {
            "host": "snowflake", "port": None, "db": self.sf.database,
            "driver": None, "user": self.sf.user,
            "tcp_reachable": None, "odbc_connected": False,
            "query_ok": False, "row_count_test": None,
            "table_columns": None, "col_conso_resolved": "GRID_ENERGY_CONSO_PER_DAY",
            "sample_5_rows": None, "mode_guess": "sum", "error": None,
        }
        try:
            conn = self.sf._connect()
            result["odbc_connected"] = True
            conn.cursor().execute("SELECT 1")
            result["query_ok"] = True
        except Exception as e:
            result["error"] = f"Snowflake: {e}"
            return result

        try:
            cursor = conn.cursor()
            db_schema = f"{self.sf.database}.{self.sf.schema}"
            cursor.execute(f"SELECT * FROM {db_schema}.GRID_REPORT LIMIT 0")
            result["table_columns"] = [d[0] for d in cursor.description]
            cursor.execute(f"""
                SELECT SITE_ID, DATE, GRID_ENERGY_CONSO_PER_DAY
                FROM {db_schema}.GRID_REPORT
                WHERE GRID_ENERGY_CONSO_PER_DAY IS NOT NULL AND GRID_ENERGY_CONSO_PER_DAY > 0
                ORDER BY SITE_ID, DATE
                LIMIT 5
            """)
            rows = cursor.fetchall()
            result["sample_5_rows"] = [
                {"site_id": r[0], "date": str(r[1]), "conso": float(r[2])}
                for r in rows
            ]
            result["row_count_test"] = len(rows)
        except Exception as e:
            result["error"] = f"Table check: {e}"
        finally:
            try: conn.close()
            except Exception: pass

        return result
