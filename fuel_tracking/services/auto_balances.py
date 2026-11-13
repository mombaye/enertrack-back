"""Calcul automatique des bilans de stock : derniers mois complets (tâche de nuit, commande fuel_auto_balances)
et mois touchés par un import accepté (tâche lancée juste après l'import)."""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from django.utils import timezone

from fuel_tracking.services import stock_period_balance as SB
from fuel_tracking.services.period_utils import day_bounds


def last_complete_months(n: int, today: date | None = None) -> list[tuple[date, date]]:
    today = today or timezone.localdate()
    first = today.replace(day=1)
    out = []
    for _ in range(n):
        end = first - timedelta(days=1)
        start = end.replace(day=1)
        out.append((start, end))
        first = start
    return out


def compute_recent_months(n: int = 2, today: date | None = None) -> list[dict]:
    return compute_months(last_complete_months(n, today))


# ── Recalcul juste après un import accepté (sans attendre la tâche de nuit) ─────────────────────────────

MAX_MOIS_APRES_IMPORT = 3
# Les objectifs (target) n'entrent pas dans le bilan de stock : pas de recalcul pour ce type.
TYPES_SANS_BILAN = {"TARGET_FUEL"}


def months_for_period(start: date | None, end: date | None, today: date | None = None, n_defaut: int = 2) -> list[tuple[date, date]]:
    """
    Mois COMPLETS touchés par la période déclarée d'un lot (au plus MAX_MOIS_APRES_IMPORT, les plus récents).
    Le mois en cours est exclu (ses bilans sont faits une fois le mois terminé). Sans période déclarée
    (ex. modèles de cuves) : les n_defaut derniers mois complets, comme la tâche de nuit.
    """
    today = today or timezone.localdate()
    if start is None or end is None:
        return last_complete_months(n_defaut, today)
    if end < start:
        start, end = end, start
    current = today.replace(day=1)
    out = []
    m = start.replace(day=1)
    while m <= end and m < current:
        out.append((m, m.replace(day=calendar.monthrange(m.year, m.month)[1])))
        m = (m + timedelta(days=32)).replace(day=1)
    return sorted(out[-MAX_MOIS_APRES_IMPORT:], reverse=True)


def compute_months(months: list[tuple[date, date]]) -> list[dict]:
    res = []
    for start, end in months:
        s, e = day_bounds(start, end)
        rows = SB.compute_balances(s, e)
        res.append({"debut": start.isoformat(), "fin": end.isoformat(), "sites": len(rows), "calculables": sum(1 for r in rows if r.get("consommation_reelle_l") is not None)})
    return res
