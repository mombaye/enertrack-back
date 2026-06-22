# financial/services/conso_service.py
"""
FinancialConsoService
─────────────────────
Récupère les consommations (FMS/ACM) et la production solaire
pour un site sur une plage de mois.

Stratégie par mois :
  1. LOCAL  → CertificationResult (déjà calculé lors d'un batch de certification)
  2. REMOTE → EfmsService.get_conso_acm / get_conso_periode (fallback SQL Server)
  3. SOLAR  → SQL2-ProdDB.[silver].[fact_solar_mth] (toujours requête directe)

Retourne un dict {month: ConsoMonthData} consommable directement dans SiteMargeDetailView.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConsoMonthData:
    month:              int
    period_label:       str               # "2025-03"

    # FMS / ACM
    conso_fms_kwh:      Optional[Decimal] = None   # Grid Report (FMS)
    conso_acm_kwh:      Optional[Decimal] = None   # AC Meter
    ratio_fms:          Optional[Decimal] = None   # ratio FMS / Sénélec
    fms_available:      bool              = False
    acm_available:      bool              = False
    fms_source:         str               = "none"  # "local" | "remote" | "none"
    acm_source:         str               = "none"

    # Solar
    solar_kwh:          Optional[float]   = None
    unavail_hours:      Optional[float]   = None
    solar_source:       str               = "none"  # "remote" | "none"

    # Meta
    date_debut:         Optional[date]    = None
    date_fin:           Optional[date]    = None


# ─────────────────────────────────────────────────────────────────────────────
# HELPER : connexion SQL2-ProdDB pour solar
# ─────────────────────────────────────────────────────────────────────────────

def _build_sql2_conn_string() -> str:
    host     = getattr(settings, "EFMS_SQL_HOST",   "172.30.0.149")
    port     = int(getattr(settings, "EFMS_SQL_PORT", 1433))
    user     = getattr(settings, "EFMS_SQL_USER",   "")
    password = getattr(settings, "EFMS_SQL_PASSWORD", "")
    driver   = getattr(settings, "EFMS_SQL_DRIVER", "ODBC Driver 17 for SQL Server")
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE=SQL2-ProdDB;"
        f"UID={user};PWD={password};"
        "TrustServerCertificate=yes;"
        "Connection Timeout=8;"
    )


# ─────────────────────────────────────────────────────────────────────────────
# SERVICE
# ─────────────────────────────────────────────────────────────────────────────

class FinancialConsoService:
    """
    Usage dans SiteMargeDetailView :

        svc = FinancialConsoService(site_id, year, month_start, month_end)
        conso_map = svc.fetch()   # → dict[int, ConsoMonthData]

        for row in billing_rows:
            data = conso_map.get(row["month"], ConsoMonthData(row["month"], row["period"]))
            row["conso_fms_kwh"]   = str(data.conso_fms_kwh) if data.conso_fms_kwh else None
            row["conso_acm_kwh"]   = str(data.conso_acm_kwh) if data.conso_acm_kwh else None
            row["ratio_fms"]       = str(data.ratio_fms)     if data.ratio_fms     else None
            row["fms_available"]   = data.fms_available
            row["acm_available"]   = data.acm_available
            row["solar_kwh"]       = data.solar_kwh
            row["unavail_hours"]   = data.unavail_hours
            row["conso_fms_source"] = data.fms_source
    """

    # Timeout pour les requêtes SQL Remote (en secondes)
    REMOTE_TIMEOUT = 10

    def __init__(
        self,
        site_id: str,
        year: int,
        month_start: int,
        month_end: int,
        billing_rows: list[dict] | None = None,
    ):
        self.site_id     = site_id
        self.year        = year
        self.month_start = month_start
        self.month_end   = month_end
        # billing_rows fournit les nb_jours et period par mois
        self._billing_index: dict[int, dict] = {
            r["month"]: r for r in (billing_rows or [])
        }

    # ── Public entry point ────────────────────────────────────────────────────

    def fetch(self) -> dict[int, ConsoMonthData]:
        """
        Retourne un dict {month → ConsoMonthData} pour tous les mois de la plage.
        """
        months = list(range(self.month_start, self.month_end + 1))
        result: dict[int, ConsoMonthData] = {}

        # Initialise les entrées vides
        for m in months:
            label = f"{self.year}-{m:02d}"
            result[m] = ConsoMonthData(month=m, period_label=label)

        # ── ÉTAPE 1 : données locales (CertificationResult) ───────────────────
        missing_months = self._fill_from_local(result, months)

        # ── ÉTAPE 2 : fallback remote pour les mois sans données locales ──────
        if missing_months:
            logger.info(
                "[ConsoService] %s — %d mois sans données locales → requête eFMS remote : %s",
                self.site_id, len(missing_months), missing_months,
            )
            self._fill_from_remote(result, missing_months)

        # ── ÉTAPE 3 : production solaire (toujours remote) ────────────────────
        self._fill_solar(result, months)

        return result

    @staticmethod
    def fetch_bulk_for_list(
        site_ids: list[str],
        year_start: int,
        month_start: int,
        year_end: int,
        month_end: int,
    ) -> dict[tuple, dict]:
        """
        Requête bulk directe sur SQL1-ProdDB (Grid + ACM) et SQL2-ProdDB (Solar).
 
        Retourne :
            { (site_id, year, month): {
                "fms_grid_kwh": Decimal|None,
                "fms_acm_kwh":  Decimal|None,
                "solar_kwh":    float|None,
            }}
        """
        import calendar as _cal
        from datetime import date as _date
        import pyodbc
 
        if not site_ids:
            return {}
 
        CHUNK_SIZE = 400
        MIN_PTS    = 3
        MIN_DENSE  = 20
 
        # ── Connexions ────────────────────────────────────────────────────────
        host     = getattr(settings, "EFMS_SQL_HOST",     "172.30.0.149")
        port     = int(getattr(settings, "EFMS_SQL_PORT",  1433))
        user     = getattr(settings, "EFMS_SQL_USER",     "")
        password = getattr(settings, "EFMS_SQL_PASSWORD", "")
        driver   = getattr(settings, "EFMS_SQL_DRIVER",   "ODBC Driver 17 for SQL Server")
        timeout  = int(getattr(settings, "EFMS_SQL_TIMEOUT", 15))
 
        def _conn_str(database: str) -> str:
            return (
                f"DRIVER={{{driver}}};"
                f"SERVER={host},{port};"
                f"DATABASE={database};"
                f"UID={user};PWD={password};"
                "TrustServerCertificate=yes;"
                f"Connection Timeout={timeout};"
            )
 
        TABLE_GRID = "[SQL1-ProdDB].[dbo].[silver.gfms_Grid_Report_day]"
        TABLE_ACM  = "[SQL1-ProdDB].[dbo].[silver.gfms_AC_Meter_Report_day]"
 
        d_start = _date(year_start, month_start, 1)
        d_end   = _date(year_end, month_end, _cal.monthrange(year_end, month_end)[1])
 
        # Toutes les clés (year, month) dans la plage — pour la requête solar
        period_keys: list[tuple[int, int]] = []
        y, m = year_start, month_start
        while (y, m) <= (year_end, month_end):
            period_keys.append((y, m))
            if m == 12:
                y, m = y + 1, 1
            else:
                m += 1
 
        result: dict[tuple, dict] = {}
 
        # ── SQL1 : Grid + ACM ─────────────────────────────────────────────────
        conn1 = None
        try:
            conn1  = pyodbc.connect(_conn_str("SQL1-ProdDB"), timeout=timeout)
            cursor = conn1.cursor()
 
            # Résolution colonne Grid
            COL_CANDIDATES = [
                "GRID_ENERGY_CONSO_PER_DAY", "Grid Energy Conso per Day",
                "Grid_Energy_Conso_per_day", "Grid_Energy_Conso_per_Day",
                "GridEnergyConso", "energy_kwh", "conso_kwh",
            ]
            cursor.execute(f"SELECT TOP 0 * FROM {TABLE_GRID}")
            db_cols = {d[0].lower(): d[0] for d in cursor.description}
            col_grid = next(
                (db_cols[c.lower()] for c in COL_CANDIDATES if c.lower() in db_cols),
                next((v for k, v in db_cols.items()
                      if any(x in k for x in ("conso", "energy", "kwh"))), None),
            )
 
            if col_grid:
                for chunk_start in range(0, len(site_ids), CHUNK_SIZE):
                    chunk = site_ids[chunk_start: chunk_start + CHUNK_SIZE]
                    ph    = ",".join("?" * len(chunk))
 
                    # GRID
                    cursor.execute(f"""
                        SELECT
                            site_id,
                            YEAR([Date])                           AS yr,
                            MONTH([Date])                          AS mo,
                            SUM(TRY_CAST([{col_grid}] AS FLOAT))  AS grid_kwh,
                            COUNT([Date])                          AS nb_pts
                        FROM {TABLE_GRID}
                        WHERE site_id IN ({ph})
                          AND [Date] >= ? AND [Date] <= ?
                          AND [{col_grid}] IS NOT NULL
                          AND TRY_CAST([{col_grid}] AS FLOAT) > 0.1
                        GROUP BY site_id, YEAR([Date]), MONTH([Date])
                    """, (*chunk, d_start, d_end))
 
                    for row in cursor.fetchall():
                        sid, yr, mo, kwh, nb = row[0], int(row[1]), int(row[2]), row[3], int(row[4])
                        key = (sid, yr, mo)
                        if key not in result:
                            result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None}
                        if kwh is not None and nb >= MIN_PTS:
                            days_m = _cal.monthrange(yr, mo)[1]
                            val = Decimal(str(kwh))
                            if nb < MIN_DENSE:
                                val = (val / Decimal(str(nb)) * days_m)
                            result[key]["fms_grid_kwh"] = val.quantize(Decimal("0.001"))
 
                    # ACM  (act_energy_p = index cumulatif → MAX - MIN)
                    cursor.execute(f"""
                        SELECT
                            site_id,
                            YEAR([Date])                                AS yr,
                            MONTH([Date])                               AS mo,
                            MAX(act_energy_p) - MIN(act_energy_p)      AS acm_kwh,
                            COUNT([Date])                               AS nb_pts,
                            DATEDIFF(day, MIN([Date]), MAX([Date]))     AS span_days
                        FROM {TABLE_ACM}
                        WHERE site_id IN ({ph})
                          AND [Date] >= ? AND [Date] <= ?
                          AND act_energy_p IS NOT NULL AND act_energy_p > 0
                        GROUP BY site_id, YEAR([Date]), MONTH([Date])
                    """, (*chunk, d_start, d_end))
 
                    for row in cursor.fetchall():
                        sid   = row[0]
                        yr    = int(row[1])
                        mo    = int(row[2])
                        delta = row[3]
                        nb    = int(row[4])
                        span  = int(row[5]) if row[5] else 1
                        key   = (sid, yr, mo)
                        if key not in result:
                            result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None}
                        if delta is not None and delta > 0 and nb >= MIN_PTS:
                            days_m = _cal.monthrange(yr, mo)[1]
                            d = Decimal(str(delta))
                            if nb >= MIN_DENSE:
                                result[key]["fms_acm_kwh"] = d.quantize(Decimal("0.001"))
                            else:
                                result[key]["fms_acm_kwh"] = (
                                    d / Decimal(str(max(1, span))) * days_m
                                ).quantize(Decimal("0.001"))
 
        except Exception as e:
            logger.warning("[ConsoService bulk] Erreur SQL1 Grid/ACM : %s", e)
        finally:
            if conn1:
                try:
                    conn1.close()
                except Exception:
                    pass
 
        # ── SQL2 : Solar ──────────────────────────────────────────────────────
        # fact_solar_mth a une ligne par (site_id, month_year)
        # month_year format : "2025-03" ou datetime
        conn2 = None
        try:
            period_start = f"{year_start}-{month_start:02d}"
            period_end   = f"{year_end}-{month_end:02d}"
 
            conn2  = pyodbc.connect(_conn_str("SQL2-ProdDB"), timeout=timeout)
            cursor = conn2.cursor()
 
            for chunk_start in range(0, len(site_ids), CHUNK_SIZE):
                chunk = site_ids[chunk_start: chunk_start + CHUNK_SIZE]
                ph    = ",".join("?" * len(chunk))
 
                cursor.execute(f"""
                    SELECT
                        site_id,
                        month_year,
                        solar_production_kwh_mth,
                        monitoring_unavailability_hours
                    FROM [SQL2-ProdDB].[silver].[fact_solar_mth]
                    WHERE site_id IN ({ph})
                      AND month_year >= ?
                      AND month_year <= ?
                """, (*chunk, period_start, period_end))
 
                for row in cursor.fetchall():
                    sid      = row[0]
                    my       = row[1]   # datetime ou string "2025-03"
                    solar_v  = row[2]
                    unavail  = row[3]
 
                    try:
                        if hasattr(my, "month"):
                            yr, mo = my.year, my.month
                        else:
                            yr_s, mo_s = str(my).split("-")
                            yr, mo = int(yr_s), int(mo_s)
                    except Exception:
                        continue
 
                    key = (sid, yr, mo)
                    if key not in result:
                        result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None}
 
                    if solar_v is not None:
                        result[key]["solar_kwh"]    = float(solar_v)
                    if unavail is not None:
                        result[key]["unavail_hours"] = float(unavail)
 
        except Exception as e:
            logger.warning("[ConsoService bulk] Erreur SQL2 Solar : %s", e)
        finally:
            if conn2:
                try:
                    conn2.close()
                except Exception:
                    pass
 
        return result
 
 

    # ── ÉTAPE 1 : local ──────────────────────────────────────────────────────

    def _fill_from_local(
        self, result: dict[int, ConsoMonthData], months: list[int]
    ) -> list[int]:
        """
        Lit les CertificationResult existants.
        Retourne la liste des mois pour lesquels aucune donnée n'a été trouvée.
        """
        try:
            from certification.models import CertificationResult

            cert_qs = (
                CertificationResult.objects
                .filter(
                    site__site_id=self.site_id,
                    cert_batch__echeance_year=self.year,
                    cert_batch__echeance_month__gte=self.month_start,
                    cert_batch__echeance_month__lte=self.month_end,
                )
                .select_related("cert_batch")
                .order_by("cert_batch__echeance_month", "-computed_at")
            )

            # Grouper par mois — garder seulement le plus récent par mois
            seen = set()
            for c in cert_qs:
                m = c.cert_batch.echeance_month
                if m in seen:
                    continue
                seen.add(m)

                data = result[m]

                # FMS (Grid)
                if c.conso_fms_periode is not None:
                    data.conso_fms_kwh  = c.conso_fms_periode
                    data.fms_available  = True
                    data.fms_source     = "local"

                # ACM
                acm_val = getattr(c, "conso_acm_periode", None)
                if acm_val is not None:
                    data.conso_acm_kwh  = acm_val
                    data.acm_available  = True
                    data.acm_source     = "local"

                # Ratio FMSA
                if c.ratio_fms_periode is not None:
                    data.ratio_fms = c.ratio_fms_periode

                # Flags disponibilité
                data.fms_available = data.fms_available or bool(c.fms_available)
                data.acm_available = data.acm_available or bool(c.acm_available)

        except Exception as e:
            logger.warning("[ConsoService] Erreur lecture CertificationResult : %s", e)

        # Mois sans aucune donnée locale (ni FMS ni ACM)
        missing = [
            m for m in months
            if result[m].fms_source == "none" and result[m].acm_source == "none"
        ]
        return missing

    # ── ÉTAPE 2 : remote eFMS ────────────────────────────────────────────────

    def _fill_from_remote(
        self, result: dict[int, ConsoMonthData], months: list[int]
    ) -> None:
        """
        Pour chaque mois manquant, interroge eFMS directement (ACM + Grid).
        Utilise les méthodes ponctuelles d'EfmsService (pas de prefetch —
        on est sur 1 site, pas un batch de 3000).
        """
        try:
            from certification.services.efms import EfmsService, EfmsConnectionError, EfmsQueryError
            svc = EfmsService()
        except ImportError:
            logger.warning("[ConsoService] EfmsService non disponible")
            return

        for m in months:
            data       = result[m]
            bill_row   = self._billing_index.get(m, {})
            date_debut, date_fin = self._month_bounds(m, bill_row)

            if date_debut is None or date_fin is None:
                continue

            data.date_debut = date_debut
            data.date_fin   = date_fin

            # ── ACM (priorité) ───────────────────────────────────────────────
            try:
                acm_periode, _ = svc.get_conso_acm(
                    self.site_id, date_debut, date_fin
                )
                if acm_periode is not None:
                    data.conso_acm_kwh  = acm_periode
                    data.acm_available  = True
                    data.acm_source     = "remote"
            except (EfmsConnectionError, EfmsQueryError) as e:
                logger.warning("[ConsoService] ACM remote %s m=%d : %s", self.site_id, m, e)

            # ── Grid / FMS ───────────────────────────────────────────────────
            try:
                fms_periode, fms_mode = svc.get_conso_periode(
                    self.site_id, date_debut, date_fin
                )
                if fms_periode is not None:
                    data.conso_fms_kwh  = fms_periode
                    data.fms_available  = True
                    data.fms_source     = f"remote:{fms_mode}"
            except (EfmsConnectionError, EfmsQueryError) as e:
                logger.warning("[ConsoService] Grid remote %s m=%d : %s", self.site_id, m, e)

            # ── Ratio FMS / Facturée (si on a les deux) ──────────────────────
            if data.conso_fms_kwh is not None:
                facturee_str = bill_row.get("energie")
                if facturee_str:
                    try:
                        conso_fact = Decimal(str(facturee_str))
                        if conso_fact > 0:
                            data.ratio_fms = (data.conso_fms_kwh / conso_fact).quantize(
                                Decimal("0.001")
                            )
                    except Exception:
                        pass

    # ── ÉTAPE 3 : solar ──────────────────────────────────────────────────────

    def _fill_solar(
        self, result: dict[int, ConsoMonthData], months: list[int]
    ) -> None:
        """
        Requête directe sur SQL2-ProdDB.[silver].[fact_solar_mth].
        Une seule connexion pour tous les mois de la plage.
        """
        try:
            import pyodbc

            period_start = f"{self.year}-{self.month_start:02d}"
            period_end   = f"{self.year}-{self.month_end:02d}"

            conn = pyodbc.connect(_build_sql2_conn_string(), timeout=self.REMOTE_TIMEOUT)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    month_year,
                    solar_production_kwh_mth,
                    monitoring_unavailability_hours
                FROM [SQL2-ProdDB].[silver].[fact_solar_mth]
                WHERE site_id = ?
                  AND month_year >= ?
                  AND month_year <= ?
                ORDER BY month_year
                """,
                (self.site_id, period_start, period_end),
            )

            for row_sql in cursor.fetchall():
                # month_year : "2025-03" ou datetime selon le driver
                my = row_sql[0]
                try:
                    if hasattr(my, "month"):
                        m = my.month
                    else:
                        _, m_str = str(my).split("-")
                        m = int(m_str)
                except (ValueError, AttributeError):
                    continue

                if m not in result:
                    continue

                data = result[m]
                solar_val  = row_sql[1]
                unavail    = row_sql[2]

                data.solar_kwh     = float(solar_val)  if solar_val  is not None else None
                data.unavail_hours = float(unavail)    if unavail     is not None else None
                data.solar_source  = "remote" if data.solar_kwh is not None else "none"

            conn.close()

        except Exception as e:
            logger.warning(
                "[ConsoService] Impossible de charger fact_solar_mth pour %s : %s",
                self.site_id, e,
            )

    # ── Helper : bornes d'un mois ─────────────────────────────────────────────

    def _month_bounds(
        self, month: int, bill_row: dict
    ) -> tuple[Optional[date], Optional[date]]:
        """
        Retourne (date_debut, date_fin) pour le mois.
        Préfère les dates réelles de la facture si disponibles.
        """
        import calendar as _cal

        # 1. Dates réelles depuis la facture (si billing_rows fourni)
        # Les billing_rows ont first_period_start / last_period_end dans
        # ContractMonth — ils ne sont pas exposés directement ici,
        # mais nb_jours peut guider.
        # On utilise le mois calendaire comme référence par défaut.
        try:
            last_day = _cal.monthrange(self.year, month)[1]
            d_start  = date(self.year, month, 1)
            d_end    = date(self.year, month, last_day)
            return d_start, d_end
        except Exception:
            return None, None