# certification/services/efms.py

import time
import logging
from datetime import date
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

    # Schéma = dbo, nom de table avec point dans le nom
    TABLE_GRID = "[SQL1-ProdDB].[dbo].[silver.gfms_Grid_Report_day]"

    # Nom réel confirmé par introspection sur SQL1-ProdDB
    _COL_CONSO_CANDIDATES = [
        "Grid_Energy_Conso_per_day",        # ✓ confirmé
        "Grid Energy Conso per Day",        # ancienne variante avec espaces
        "Grid_Energy_Conso_per_Day",        # variante casse
        "GridEnergyConso",
        "energy_kwh",
        "conso_kwh",
    ]
    _col_conso_resolved: Optional[str] = None  # cache de classe

    def __init__(self, cert_batch=None):
        self.cert_batch  = cert_batch
        self.host        = getattr(settings, "EFMS_SQL_HOST",     "172.30.0.149")
        self.port        = int(getattr(settings, "EFMS_SQL_PORT",  1433))
        self.db          = getattr(settings, "EFMS_SQL_DB",       "SQL1-ProdDB")
        self.user        = getattr(settings, "EFMS_SQL_USER",     "")
        self.password    = getattr(settings, "EFMS_SQL_PASSWORD", "")
        self.driver      = getattr(settings, "EFMS_SQL_DRIVER",   "ODBC Driver 17 for SQL Server")
        self.timeout     = int(getattr(settings, "EFMS_SQL_TIMEOUT",     10))
        self.max_retries = int(getattr(settings, "EFMS_SQL_MAX_RETRIES", 2))

        # Peut être surchargé via settings pour éviter la résolution auto
        col_override = getattr(settings, "EFMS_SQL_COL_CONSO", None)
        if col_override:
            EfmsService._col_conso_resolved = col_override

    # ── Résolution dynamique du nom de colonne ────────────────────────────────

    def _resolve_col_conso(self, conn) -> str:
        """
        Résout une fois le vrai nom de la colonne énergie dans la table Grid.
        Résultat mis en cache sur la classe pour éviter une requête par facture.
        """
        if EfmsService._col_conso_resolved:
            return EfmsService._col_conso_resolved

        cursor = conn.cursor()
        # Récupère tous les noms de colonnes de la table
        cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
        columns = [d[0] for d in cursor.description]

        logger.info(f"[eFMS] Colonnes de {self.TABLE_GRID}: {columns}")

        # Cherche parmi les candidates (insensible à la casse)
        columns_lower = {c.lower(): c for c in columns}
        for candidate in self._COL_CONSO_CANDIDATES:
            match = columns_lower.get(candidate.lower())
            if match:
                EfmsService._col_conso_resolved = match
                logger.info(f"[eFMS] Colonne conso résolue → [{match}]")
                return match

        # Fallback : cherche par mots-clés
        for col in columns:
            col_l = col.lower()
            if "conso" in col_l or "energy" in col_l or "kwh" in col_l:
                EfmsService._col_conso_resolved = col
                logger.warning(f"[eFMS] Colonne conso heuristique → [{col}]")
                return col

        raise EfmsQueryError(
            f"Impossible de trouver la colonne énergie. "
            f"Colonnes disponibles : {columns}. "
            f"Ajoutez EFMS_SQL_COL_CONSO='NomExact' dans .env"
        )

    def _col(self, conn) -> str:
        """Retourne [NomColonne] entre crochets."""
        name = self._resolve_col_conso(conn)
        return f"[{name}]"

    # ── Connexion ─────────────────────────────────────────────────────────────

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
                    duration_ms=duration_ms,
                    host=self.host,
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
                    cert_batch=self.cert_batch,
                    status=log_status,
                    duration_ms=duration_ms,
                    host=self.host,
                    error=error_msg[:500],
                )

            except pyodbc.Error as e:
                duration_ms = int((time.monotonic() - t0) * 1000)
                error_msg = str(e)
                last_error = e
                logger.error(f"[eFMS] Erreur SQL: {error_msg[:200]}")
                EfmsConnectionLog.objects.create(
                    cert_batch=self.cert_batch,
                    status=EfmsConnectionLog.Status.SQL_ERROR,
                    duration_ms=duration_ms,
                    host=self.host,
                    error=error_msg[:500],
                )

            if attempt < self.max_retries:
                time.sleep(2 * attempt)

        raise EfmsConnectionError(
            f"Impossible de joindre eFMS après {self.max_retries} tentatives: {last_error}"
        )

    # ── Requêtes métier ───────────────────────────────────────────────────────

    def get_conso_periode(
        self,
        site_id: str,
        date_debut: date,
        date_fin: date,
    ) -> Optional[Decimal]:
        conn = None
        try:
            conn = self._open_connection()
            col = self._col(conn)
            sql = f"""
                SELECT SUM({col}) AS conso_periode
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] >= ?
                  AND [Date] <= ?
                  AND {col} IS NOT NULL
                  AND {col} > 0
            """
            cursor = conn.cursor()
            cursor.execute(sql, (site_id, date_debut, date_fin))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return Decimal(str(row[0]))
            return None
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_periode: {e}") from e
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def get_conso_last_complete_month(
        self,
        site_id: str,
        before_date: date,
        min_days: int = 20,
    ) -> tuple[Optional[Decimal], Optional[date]]:
        conn = None
        try:
            conn = self._open_connection()
            col = self._col(conn)
            sql = f"""
                SELECT TOP 1
                    YEAR([Date])  AS yr,
                    MONTH([Date]) AS mo,
                    SUM({col})    AS conso_mensuelle,
                    COUNT([Date]) AS nb_jours
                FROM {self.TABLE_GRID}
                WHERE site_id = ?
                  AND [Date] < ?
                  AND {col} IS NOT NULL
                  AND {col} > 0
                GROUP BY YEAR([Date]), MONTH([Date])
                HAVING COUNT([Date]) >= ?
                ORDER BY YEAR([Date]) DESC, MONTH([Date]) DESC
            """
            cursor = conn.cursor()
            cursor.execute(sql, (site_id, before_date, min_days))
            row = cursor.fetchone()
            if row and row[2] is not None:
                ref_date = date(int(row[0]), int(row[1]), 1)
                return Decimal(str(row[2])), ref_date
            return None, None
        except EfmsConnectionError:
            raise
        except Exception as e:
            raise EfmsQueryError(f"get_conso_last_complete_month: {e}") from e
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

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
                try:
                    conn.close()
                except Exception:
                    pass

    # ── Diagnostic ────────────────────────────────────────────────────────────

    def diagnose(self) -> dict:
        import socket
        result = {
            "host": self.host, "port": self.port, "db": self.db,
            "user": self.user, "driver": self.driver,
            "tcp_reachable": False, "odbc_connected": False,
            "query_ok": False, "table_columns": None,
            "col_conso_resolved": None, "row_count_test": None,
            "error": None,
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
            result["error"] = f"ODBC/Query: {e}"
            if conn:
                conn.close()
            return result

        try:
            # Liste toutes les colonnes
            cursor = conn.cursor()
            cursor.execute(f"SELECT TOP 0 * FROM {self.TABLE_GRID}")
            cols = [d[0] for d in cursor.description]
            result["table_columns"] = cols

            # Résout la colonne conso
            col = self._resolve_col_conso(conn)
            result["col_conso_resolved"] = col

            # Test lecture réelle
            cursor.execute(
                f"SELECT TOP 1 site_id, [Date], [{col}] FROM {self.TABLE_GRID}"
            )
            row = cursor.fetchone()
            result["row_count_test"] = str(row) if row else "table vide"

        except Exception as e:
            result["error"] = f"Table/col check: {e}"
        finally:
            try:
                conn.close()
            except Exception:
                pass

        return result