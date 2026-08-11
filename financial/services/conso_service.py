# financial/services/conso_service.py
"""
FinancialConsoService
─────────────────────
Récupère les consommations (FMS/ACM) et la production solaire
pour un site sur une plage de mois.

Stratégie par mois :
  1. LOCAL  → CertificationResult (déjà calculé lors d'un batch de certification)
  2. SYNC   → FinancialConsoMonthly (synchronisé depuis Snowflake par la commande
              `sync_financial_conso` — plus AUCUN appel Snowflake live dans le
              chemin de requête HTTP, cf. migration perf du 2026-07)

Depuis le 2026-08 : plus AUCUNE source SQL Server — le Solaire venait de
SQL2-ProdDB.silver.fact_solar_mth (en pratique injoignable, jamais mis à
jour dans cet environnement) et a été remplacé par une table Snowflake
équivalente, sur la même base que Grid/ACM (voir `_fetch_remote_bulk`).

`_fetch_remote_bulk` (les requêtes live Snowflake, chunkées 400 sites)
n'est plus appelée que par la commande de synchronisation
`financial/management/commands/sync_financial_conso.py` — jamais depuis une vue.

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
    fms_source:         str               = "none"  # "local" | "synced" | "none"
    acm_source:         str               = "none"

    # Solar
    solar_kwh:          Optional[float]   = None
    unavail_hours:      Optional[float]   = None
    solar_source:       str               = "none"  # "synced" | "none"

    # Meta
    date_debut:         Optional[date]    = None
    date_fin:           Optional[date]    = None


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

    # Timeout pour les requêtes SQL Remote (en secondes) — utilisé par _fetch_remote_bulk
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

        # ── ÉTAPE 2 : FinancialConsoMonthly (synchronisé) pour les mois manquants ──
        if missing_months:
            self._fill_from_sync(result, missing_months)

        # ── ÉTAPE 3 : production solaire (synchronisée) ────────────────────────
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
        Lecture bulk Postgres (FinancialConsoMonthly) pour un ensemble de sites
        sur une plage de mois (inter-années supporté). Remplace l'ancienne
        version qui interrogeait Snowflake/SQL Server en direct à chaque appel.

        Retourne :
            { (site_id, year, month): {
                "fms_grid_kwh":  Decimal|None,
                "fms_acm_kwh":   Decimal|None,
                "solar_kwh":     float|None,
                "unavail_hours": float|None,
            }}
        """
        from financial.models import FinancialConsoMonthly

        if not site_ids:
            return {}

        qs = FinancialConsoMonthly.objects.filter(
            site__site_id__in=site_ids,
        ).filter(
            _period_filter(year_start, month_start, year_end, month_end)
        ).values(
            "site__site_id", "year", "month",
            "fms_grid_kwh", "fms_acm_kwh", "solar_kwh", "unavail_hours",
        )

        result: dict[tuple, dict] = {}
        for row in qs:
            key = (row["site__site_id"], row["year"], row["month"])
            result[key] = {
                "fms_grid_kwh":  row["fms_grid_kwh"],
                "fms_acm_kwh":   row["fms_acm_kwh"],
                "solar_kwh":     float(row["solar_kwh"]) if row["solar_kwh"] is not None else None,
                "unavail_hours": float(row["unavail_hours"]) if row["unavail_hours"] is not None else None,
            }
        return result

    @staticmethod
    def _fetch_remote_bulk(
        site_ids: list[str],
        year_start: int,
        month_start: int,
        year_end: int,
        month_end: int,
    ) -> tuple[dict[tuple, dict], dict[str, str | None]]:
        """
        Requête bulk EN DIRECT, exclusivement Snowflake — plus aucune source
        SQL Server (cf. bascule du 2026-08 : le Solaire venait de SQL2-ProdDB.
        silver.fact_solar_mth, injoignable en pratique et non mis à jour ;
        remplacé par DB_GFMS_PROD.GOLD.JOIN_SOLAR_PRODUCTION_AND_MONITORING_
        AVAILABILITY, sur la même base Snowflake que le module carburant,
        vérifié à jour au jour le jour).

        N'est appelée QUE par `sync_financial_conso` (management command) pour
        alimenter FinancialConsoMonthly — plus jamais depuis une vue HTTP.

        Retourne (result, errors) :
            result : { (site_id, year, month): {
                "fms_grid_kwh": Decimal|None,
                "fms_acm_kwh":  Decimal|None,
                "solar_kwh":    float|None,
                "unavail_hours": float|None,
                "grid_mode":    "exact"|"extrapol"|"none",
            }}
            errors : {"snowflake": str|None, "solar": str|None} — message
            d'erreur si la requête correspondante a échoué, None si elle a
            répondu (même sans donnée pour la période demandée). Les 2 clés
            restent distinctes bien que sur la même connexion Snowflake : la
            table Solaire (JOIN_SOLAR_PRODUCTION_AND_MONITORING_AVAILABILITY)
            peut échouer indépendamment de GRID_REPORT/AC_METER (renommage,
            schéma modifié, etc.).
        """
        import calendar as _cal
        from datetime import date as _date

        from certification.services.snowflake_service import SnowflakeService

        if not site_ids:
            return {}, {"snowflake": None, "solar": None}

        CHUNK_SIZE = 400
        MIN_PTS    = 3
        MIN_DENSE  = 20

        d_start = _date(year_start, month_start, 1)
        d_end   = _date(year_end, month_end, _cal.monthrange(year_end, month_end)[1])

        result: dict[tuple, dict] = {}
        errors: dict[str, str | None] = {"snowflake": None, "solar": None}

        # ── Grid + ACM + Solaire — Snowflake ─────────────────────────────────
        conn1 = None
        try:
            sf = SnowflakeService()
            conn1 = sf._connect()
            cursor = conn1.cursor()
            db_schema = f"{sf.database}.{sf.schema}"

            for chunk_start in range(0, len(site_ids), CHUNK_SIZE):
                chunk = site_ids[chunk_start: chunk_start + CHUNK_SIZE]
                ph    = ",".join(["%s"] * len(chunk))

                # GRID
                cursor.execute(f"""
                    SELECT
                        SITE_ID,
                        YEAR(DATE)                           AS yr,
                        MONTH(DATE)                          AS mo,
                        SUM(GRID_ENERGY_CONSO_PER_DAY)       AS grid_kwh,
                        COUNT(DATE)                          AS nb_pts
                    FROM {db_schema}.GRID_REPORT
                    WHERE SITE_ID IN ({ph})
                      AND DATE >= %s AND DATE <= %s
                      AND GRID_ENERGY_CONSO_PER_DAY IS NOT NULL
                      AND GRID_ENERGY_CONSO_PER_DAY > 0.1
                    GROUP BY SITE_ID, YEAR(DATE), MONTH(DATE)
                """, tuple(chunk) + (d_start, d_end))

                for row in cursor.fetchall():
                    sid, yr, mo, kwh, nb = row[0], int(row[1]), int(row[2]), row[3], int(row[4])
                    key = (sid, yr, mo)
                    if key not in result:
                        result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None, "unavail_hours": None, "grid_mode": "none"}
                    if kwh is not None and nb >= MIN_PTS:
                        days_m = _cal.monthrange(yr, mo)[1]
                        val = Decimal(str(kwh))
                        if nb >= MIN_DENSE:
                            result[key]["grid_mode"] = "exact"
                        else:
                            val = (val / Decimal(str(nb)) * days_m)
                            result[key]["grid_mode"] = "extrapol"
                        result[key]["fms_grid_kwh"] = val.quantize(Decimal("0.001"))

                # ACM (index cumulatif -> MAX - MIN). COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) :
                # ~39 sites (sur ~7900) ne remontent jamais ACT_ENERGY_P et ne reportent
                # que sous ACM_ENERGY_P (vérifié en base) — sans ce repli, ces sites
                # n'ont jamais de conso ACM synchronisée.
                cursor.execute(f"""
                    SELECT
                        SITE_ID,
                        YEAR(DATE)                                AS yr,
                        MONTH(DATE)                               AS mo,
                        MAX(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) - MIN(COALESCE(ACT_ENERGY_P, ACM_ENERGY_P)) AS acm_kwh,
                        COUNT(DATE)                               AS nb_pts,
                        DATEDIFF('day', MIN(DATE), MAX(DATE))     AS span_days
                    FROM {db_schema}.AC_METER
                    WHERE SITE_ID IN ({ph})
                      AND DATE >= %s AND DATE <= %s
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) IS NOT NULL
                      AND COALESCE(ACT_ENERGY_P, ACM_ENERGY_P) > 0
                    GROUP BY SITE_ID, YEAR(DATE), MONTH(DATE)
                """, tuple(chunk) + (d_start, d_end))

                for row in cursor.fetchall():
                    sid   = row[0]
                    yr    = int(row[1])
                    mo    = int(row[2])
                    delta = row[3]
                    nb    = int(row[4])
                    span  = int(row[5]) if row[5] else 1
                    key   = (sid, yr, mo)
                    if key not in result:
                        result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None, "unavail_hours": None, "grid_mode": "none"}
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
            logger.warning("[ConsoService sync] Erreur Snowflake Grid/ACM : %s", e)
            errors["snowflake"] = str(e)
        finally:
            if conn1:
                try:
                    conn1.close()
                except Exception:
                    pass

        # ── Solaire — Snowflake (DB_GFMS_PROD.GOLD), même base que le module
        # carburant. Remplace l'ancienne source SQL2-ProdDB.silver.fact_solar_mth
        # (injoignable en pratique, cf. 2026-08). Jointure sur SITES_FILTERED_FIXED
        # (DATA_ID) fournie et vérifiée manuellement — donnée journalière, agrégée
        # ici en mensuel comme GRID_REPORT. Connexion Snowflake séparée de celle
        # du bloc Grid/ACM ci-dessus pour que ce bloc puisse réussir même si le
        # premier a échoué (ou l'inverse) — 2 échecs indépendants possibles.
        conn2 = None
        try:
            conn2 = SnowflakeService()._connect()
            cursor = conn2.cursor()

            for chunk_start in range(0, len(site_ids), CHUNK_SIZE):
                chunk = site_ids[chunk_start: chunk_start + CHUNK_SIZE]
                ph    = ",".join(["%s"] * len(chunk))

                cursor.execute(f"""
                    SELECT
                        sites.SITE_ID,
                        YEAR(production.DATE)                          AS yr,
                        MONTH(production.DATE)                         AS mo,
                        SUM(production.SOLAR_PRODUCTION_KWH_DAY)       AS solar_kwh,
                        SUM(production.MONITORING_UNAVAILABILITY_HOURS) AS unavail_h,
                        COUNT(production.DATE)                         AS nb_pts
                    FROM DB_GFMS_PROD.GOLD.JOIN_SOLAR_PRODUCTION_AND_MONITORING_AVAILABILITY production
                    JOIN DB_GFMS_PROD.GOLD.SITES_FILTERED_FIXED sites
                      ON sites.DATA_ID = production.ID
                    WHERE sites.SITE_ID IN ({ph})
                      AND production.DATE >= %s AND production.DATE <= %s
                      AND production.SOLAR_PRODUCTION_KWH_DAY IS NOT NULL
                    GROUP BY sites.SITE_ID, YEAR(production.DATE), MONTH(production.DATE)
                """, tuple(chunk) + (d_start, d_end))

                for row in cursor.fetchall():
                    sid, yr, mo, solar_v, unavail, nb = row[0], int(row[1]), int(row[2]), row[3], row[4], int(row[5])
                    key = (sid, yr, mo)
                    if key not in result:
                        result[key] = {"fms_grid_kwh": None, "fms_acm_kwh": None, "solar_kwh": None, "unavail_hours": None, "grid_mode": "none"}
                    if solar_v is not None and nb >= MIN_PTS:
                        result[key]["solar_kwh"] = float(solar_v)
                    if unavail is not None:
                        result[key]["unavail_hours"] = float(unavail)

        except Exception as e:
            logger.warning("[ConsoService sync] Erreur Snowflake Solaire : %s", e)
            errors["solar"] = str(e)
        finally:
            if conn2:
                try:
                    conn2.close()
                except Exception:
                    pass

        return result, errors

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

    # ── ÉTAPE 2 : FinancialConsoMonthly (synchronisé) ───────────────────────

    def _fill_from_sync(
        self, result: dict[int, ConsoMonthData], months: list[int]
    ) -> None:
        """
        Pour chaque mois manquant, lit FinancialConsoMonthly (Postgres) —
        alimentée par `sync_financial_conso`, plus aucun appel Snowflake ici.
        """
        from financial.models import FinancialConsoMonthly

        rows = FinancialConsoMonthly.objects.filter(
            site__site_id=self.site_id,
            year=self.year,
            month__in=months,
        )

        for row in rows:
            data = result.get(row.month)
            if data is None:
                continue

            bill_row = self._billing_index.get(row.month, {})
            data.date_debut, data.date_fin = self._month_bounds(row.month, bill_row)

            if row.fms_acm_kwh is not None:
                data.conso_acm_kwh = row.fms_acm_kwh
                data.acm_available = True
                data.acm_source = "synced"

            if row.fms_grid_kwh is not None:
                data.conso_fms_kwh = row.fms_grid_kwh
                data.fms_available = True
                data.fms_source = f"synced:{row.grid_mode}"

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
        """Lit solar_kwh/unavail_hours depuis FinancialConsoMonthly (Postgres)."""
        from financial.models import FinancialConsoMonthly

        rows = FinancialConsoMonthly.objects.filter(
            site__site_id=self.site_id,
            year=self.year,
            month__in=months,
        )

        for row in rows:
            data = result.get(row.month)
            if data is None:
                continue
            data.solar_kwh     = float(row.solar_kwh) if row.solar_kwh is not None else None
            data.unavail_hours = float(row.unavail_hours) if row.unavail_hours is not None else None
            data.solar_source  = "synced" if data.solar_kwh is not None else "none"

    # ── Helper : bornes d'un mois ─────────────────────────────────────────────

    def _month_bounds(
        self, month: int, bill_row: dict
    ) -> tuple[Optional[date], Optional[date]]:
        """
        Retourne (date_debut, date_fin) pour le mois.
        Préfère les dates réelles de la facture si disponibles.
        """
        import calendar as _cal

        try:
            last_day = _cal.monthrange(self.year, month)[1]
            d_start  = date(self.year, month, 1)
            d_end    = date(self.year, month, last_day)
            return d_start, d_end
        except Exception:
            return None, None


def _period_filter(year_start: int, month_start: int, year_end: int, month_end: int):
    """Q() inter-années pour filtrer FinancialConsoMonthly sur (year, month)."""
    from django.db.models import Q

    if year_start == year_end:
        return Q(year=year_start, month__gte=month_start, month__lte=month_end)
    return (
        Q(year=year_start, month__gte=month_start)
        | Q(year__gt=year_start, year__lt=year_end)
        | Q(year=year_end, month__lte=month_end)
    )
