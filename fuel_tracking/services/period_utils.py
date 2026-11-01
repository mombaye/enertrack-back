# fuel_tracking/services/period_utils.py
"""
Période d'analyse EXACTE du suivi carburant : toujours des JOURS ENTIERS, « date_debut 00:00:00 → date_fin 23:59:59 » (fuseau métier Africa/Dakar, UTC+0).

Cause de la rupture constatée en production : un sélecteur envoyait « 2026-08-31T23:59 » (minutes) alors que le bilan était enregistré à « 23:59:59 » ;
la recherche par égalité stricte ne retrouvait donc aucun bilan (« Aucun bilan de stock calculé ») alors que 708 sites étaient calculables. Toutes les
API normalisent maintenant leurs bornes ici et retrouvent le bilan par JOUR (début et fin), quel que soit le format (date, minutes, secondes, Z, décalage).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from django.utils import timezone

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def to_day(value) -> date | None:
    """« 2026-08-31 », « 2026-08-31T23:59 », « 2026-08-31 23:59:59 », « …Z », datetime ou date → jour métier. None si illisible."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if _DAY_RE.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


def day_bounds(d_from: date, d_to: date) -> tuple[datetime, datetime]:
    """(date_debut 00:00:00, date_fin 23:59:59) dans le fuseau métier."""
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(d_from, time.min), tz), timezone.make_aware(datetime.combine(d_to, time(23, 59, 59)), tz))


def period_from_params(debut, fin) -> tuple[date, date] | None:
    a, b = to_day(debut), to_day(fin)
    return (a, b) if a and b and a <= b else None


def balances_for_days(d_from: date, d_to: date):
    """Bilans de la période en JOURS ENTIERS (tolérant aux secondes / minutes / fuseau). Un seul bilan par site : le plus récemment calculé."""
    from fuel_tracking.models import FuelStockBalance as B

    s0, e0 = day_bounds(d_from, d_to)
    one_day = timedelta(days=1)
    return B.objects.filter(observation_start__gte=s0, observation_start__lt=s0 + one_day,
                            observation_end__gte=e0.replace(hour=0, minute=0, second=0), observation_end__lt=e0.replace(hour=0, minute=0, second=0) + one_day)


def latest_balances_by_site(d_from: date, d_to: date) -> dict:
    out: dict = {}
    for b in balances_for_days(d_from, d_to).order_by("calculated_at"):
        out[b.site_id] = b
    return out


def available_periods(limit: int = 60) -> list[dict]:
    """Périodes pour lesquelles un bilan existe (une ligne par couple jour de début / jour de fin), les plus récentes d'abord."""
    from django.db.models import Count, Q

    from fuel_tracking.models import FuelStockBalance as B

    agg: dict = {}
    for r in (B.objects.values("observation_start", "observation_end")
              .annotate(sites=Count("id"), calculables=Count("id", filter=Q(consommation_reelle_l__isnull=False)),
                        certifiables=Count("id", filter=Q(statut_bilan="PRET_A_CERTIFIER")))):
        key = (to_day(r["observation_start"]), to_day(r["observation_end"]))
        a = agg.setdefault(key, {"debut": key[0].isoformat(), "fin": key[1].isoformat(), "sites": 0, "calculables": 0, "certifiables": 0})
        a["sites"] += r["sites"]
        a["calculables"] += r["calculables"]
        a["certifiables"] += r["certifiables"]
    rows = sorted(agg.values(), key=lambda x: (x["fin"], x["debut"]), reverse=True)[:limit]
    for x in rows:
        x["libelle"] = f"{date.fromisoformat(x['debut']):%d/%m/%Y} 00:00:00 → {date.fromisoformat(x['fin']):%d/%m/%Y} 23:59:59"
    return rows


def default_period(today: date | None = None) -> tuple[date, date] | None:
    """Dernière période COMPLÈTE (fin avant aujourd'hui) pour laquelle un bilan existe ; à défaut la plus récente."""
    today = today or timezone.localdate()
    periods = available_periods()
    if not periods:
        return None
    complete = [p for p in periods if date.fromisoformat(p["fin"]) < today]
    p = (complete or periods)[0]
    return date.fromisoformat(p["debut"]), date.fromisoformat(p["fin"])
