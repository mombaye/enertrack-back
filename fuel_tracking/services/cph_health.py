# fuel_tracking/services/cph_health.py
"""
Santé du calcul CPH / conso estimée : fraîcheur Snowflake, échec de synchronisation,
référentiel, couverture du calcul et plausibilité (consommation spécifique L/kWh).
Utilisé par la tâche quotidienne (journalise les anomalies en ERROR pour l'alerting),
la commande check_cph_health et l'endpoint GET cph/health/.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.conf import settings
from django.db.models import Max

CRITIQUE = "CRITIQUE"
ALERTE = "ALERTE"


def _setting(name: str, default: float) -> float:
    return float(getattr(settings, name, default))


def freshness() -> dict:
    """Fraîcheur des faits journaliers Snowflake (léger : utilisé à chaque appel API)."""
    from fuel_tracking.models import FuelDailyFactsSyncRun, FuelSiteDailyFacts

    last_date = FuelSiteDailyFacts.objects.aggregate(v=Max("date"))["v"]
    last_run = FuelDailyFactsSyncRun.objects.order_by("-started_at").first()
    age = (date.today() - last_date).days if last_date else None
    stale_after = int(_setting("FUEL_CPH_STALE_AFTER_DAYS", 3))
    return {
        "facts_last_date": last_date,
        "facts_age_days": age,
        "facts_stale": age is None or age > stale_after,
        "stale_after_days": stale_after,
        "last_sync_failed": bool(last_run and last_run.status == FuelDailyFactsSyncRun.Status.FAILED),
        "last_sync_error": last_run.error_message if last_run and last_run.status == FuelDailyFactsSyncRun.Status.FAILED else None,
    }


def cph_health(today: date | None = None) -> dict:
    from fuel_tracking.models import CphCurve, CphInventoryMapping
    from fuel_tracking.services.cph_matching import USABLE_MATCH_STATUSES
    from fuel_tracking.services.cph_service import compute_period_summary

    today = today or date.today()
    issues: list[dict] = []
    f = freshness()
    if f["facts_last_date"] is None:
        issues.append({"niveau": CRITIQUE, "code": "FAITS_ABSENTS", "message": "Aucun fait journalier Snowflake en base."})
    elif f["facts_stale"]:
        issues.append({"niveau": CRITIQUE if f["facts_age_days"] > 7 else ALERTE, "code": "FAITS_PERIMES",
                       "message": f"Dernière donnée Snowflake : {f['facts_last_date']} ({f['facts_age_days']} j)."})
    if f["last_sync_failed"]:
        issues.append({"niveau": ALERTE, "code": "SYNC_ECHEC", "message": f"Dernière synchronisation en échec : {f['last_sync_error']}"})
    if not CphCurve.objects.exists():
        issues.append({"niveau": CRITIQUE, "code": "ABAQUE_ABSENT", "message": "Abaque CPH non importé."})
    elif not CphInventoryMapping.objects.filter(match_status__in=USABLE_MATCH_STATUSES).exists():
        issues.append({"niveau": CRITIQUE, "code": "AUCUN_MAPPING_ACTIF", "message": "Aucune correspondance plaque → courbe active."})

    metrics: dict = {}
    if f["facts_last_date"]:
        end = min(today - timedelta(days=1), f["facts_last_date"])
        rows = compute_period_summary(end - timedelta(days=6), end)
        eligible = [r for r in rows if r["curve"] is not None and r["runtime_days"] > 0]
        computed = [r for r in eligible if r["cph_days"] > 0]
        sfc_alerts = [r for r in computed if r["conso_specifique"]["alerte_estimee"]]
        coverage = len(computed) / len(eligible) if eligible else None
        metrics = {"periode": [end - timedelta(days=6), end], "sites": len(rows), "sites_eligibles": len(eligible),
                   "sites_cph": len(computed), "couverture_cph": coverage, "alertes_sfc_estimee": len(sfc_alerts)}
        min_cov = _setting("FUEL_CPH_MIN_COVERAGE", 0.5)
        if coverage is not None and coverage < min_cov:
            issues.append({"niveau": ALERTE, "code": "COUVERTURE_FAIBLE",
                           "message": f"CPH calculé sur {coverage:.0%} des sites éligibles (7 derniers jours) < {min_cov:.0%}."})
        if computed and len(sfc_alerts) / len(computed) > _setting("FUEL_CPH_MAX_SFC_ALERT_SHARE", 0.2):
            issues.append({"niveau": ALERTE, "code": "SFC_HORS_PLAGE_MASSIF",
                           "message": (f"{len(sfc_alerts)}/{len(computed)} sites avec une conso spécifique estimée hors plage : "
                                       "vérifier les unités P_DC / rendement (FUEL_CPH_P_DC_TO_KW_DIVISOR) et les courbes.")})
    return {"ok": not issues, "critique": any(i["niveau"] == CRITIQUE for i in issues),
            "issues": issues, "fraicheur": f, "metrics": metrics}
