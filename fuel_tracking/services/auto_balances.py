"""Calcul automatique des bilans de stock des derniers mois complets (appelé par la tâche planifiée et la commande fuel_auto_balances)."""
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
    res = []
    for start, end in last_complete_months(n, today):
        s, e = day_bounds(start, end)
        rows = SB.compute_balances(s, e)
        res.append({"debut": start.isoformat(), "fin": end.isoformat(), "sites": len(rows), "calculables": sum(1 for r in rows if r.get("consommation_reelle_l") is not None)})
    return res
