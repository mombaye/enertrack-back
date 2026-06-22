# ml/features/events.py
"""
Features événements exceptionnels sénégalais pour le ML.
Nécessite : pip install hijri-converter
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from ml.features.zones import normalize_zone

EVENTS = {
    "magal": (2, 18),
    "gamou": (3, 12),
    "tabaski": (12, 10),
    "korite": (10, 1),
    "tamkharit": (1, 10),
    "magal_darou": (4, 18),
    "layene": (5, 12),
}

ZONE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "magal": {
        "DIOURBEL": 1.00,
        "LOUGA": 0.65,
        "THIES": 0.60,
        "KAOLACK": 0.50,
        "DKR": 0.40,
        "SAINT-LOUIS": 0.35,
        "TAMBACOUNDA": 0.30,
        "FATICK": 0.30,
        "KAFFRINE": 0.25,
        "MATAM": 0.25,
        "ZIGUINCHOR": 0.20,
        "KOLDA": 0.20,
        "SEDHIOU": 0.15,
        "KEDOUGOU": 0.10,
    },"gamou": {
        "THIES": 1.00,
        "DIOURBEL": 0.55,
        "LOUGA": 0.50,
        "DKR": 0.35,
        "KAOLACK": 0.35,
        "FATICK": 0.30,
        "SAINT-LOUIS": 0.30,
        "KAFFRINE": 0.20,
        "TAMBACOUNDA": 0.15,
        "MATAM": 0.15,
        "ZIGUINCHOR": 0.10,
        "KOLDA": 0.10,
        "SEDHIOU": 0.10,
        "KEDOUGOU": 0.08,
    },
    "tabaski": {k: 0.50 for k in ["DKR", "THIES", "DIOURBEL", "LOUGA", "KAOLACK", "ZIGUINCHOR", "SAINT-LOUIS", "TAMBACOUNDA", "KOLDA", "FATICK", "MATAM", "KAFFRINE", "SEDHIOU", "KEDOUGOU"]},
    "korite": {k: 0.35 for k in ["DKR", "THIES", "DIOURBEL", "LOUGA", "KAOLACK", "ZIGUINCHOR", "SAINT-LOUIS", "TAMBACOUNDA", "KOLDA", "FATICK", "MATAM", "KAFFRINE", "SEDHIOU", "KEDOUGOU"]},
    "tamkharit": {k: 0.15 for k in ["DKR", "THIES", "DIOURBEL", "LOUGA", "KAOLACK", "ZIGUINCHOR", "SAINT-LOUIS", "TAMBACOUNDA", "KOLDA", "FATICK"]},
    "layene": {"DKR": 0.70},
    "magal_darou": {"DIOURBEL": 0.60, "LOUGA": 0.30, "THIES": 0.20, "KAOLACK": 0.20},
}

EVENT_WINDOW = {
    "magal": (-7, 3),
    "gamou": (-5, 2),
    "tabaski": (-3, 3),
    "korite": (-3, 2),
    "tamkharit": (-1, 1),
    "layene": (-2, 1),
    "magal_darou": (-4, 2),
}


def get_event_dates(year_start: int, year_end: int) -> List[Dict]:
    try:
        from hijri_converter import convert
    except ImportError as exc:
        raise ImportError("pip install hijri-converter") from exc

    results = []
    hijri_start = year_start + 578
    hijri_end = year_end + 580

    for h_year in range(hijri_start, hijri_end + 1):
        for event_name, (h_month, h_day) in EVENTS.items():
            try:
                g = convert.Hijri(h_year, h_month, h_day).to_gregorian()
                event_date = date(g.year, g.month, g.day)
            except Exception:
                continue

            if not (year_start <= event_date.year <= year_end):
                continue

            pre, post = EVENT_WINDOW.get(event_name, (-7, 3))
            results.append({
                "event": event_name,
                "date": event_date,
                "window_start": event_date + timedelta(days=pre),
                "window_end": event_date + timedelta(days=post),
            })

    return results



def _count_overlap_days(window_start: date, window_end: date, year: int, month: int) -> int:
    last = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end = date(year, month, last)
    lo = max(window_start, month_start)
    hi = min(window_end, month_end)
    return max(0, (hi - lo).days + 1)



def event_features_for_month(events_calendar: List[Dict], year: int, month: int, zone: str, site_name: str | None = None, site_id: str | None = None) -> dict:
    zone_norm = normalize_zone(zone, site_name=site_name, site_id=site_id)
    result = {}
    total_pressure = 0.0

    for event_name in EVENTS:
        result[f"event_{event_name}_in_month"] = 0
        result[f"event_{event_name}_days"] = 0
        result[f"event_{event_name}_pressure"] = 0.0
        result[f"event_{event_name}_pre_month"] = 0
        result[f"event_{event_name}_peak_month"] = 0

    for ev in events_calendar:
        name = ev["event"]
        overlap = _count_overlap_days(ev["window_start"], ev["window_end"], year, month)
        if overlap <= 0:
            continue

        weight = ZONE_WEIGHTS.get(name, {}).get(zone_norm, 0.15)
        pressure = round(overlap * weight, 3)

        pre_start, _ = EVENT_WINDOW.get(name, (-7, 3))
        pre_overlap = _count_overlap_days(ev["date"] + timedelta(days=pre_start), ev["date"] - timedelta(days=1), year, month)
        last = calendar.monthrange(year, month)[1]
        peak_in_month = int(date(year, month, 1) <= ev["date"] <= date(year, month, last))

        result[f"event_{name}_in_month"] = 1
        result[f"event_{name}_days"] = overlap
        result[f"event_{name}_pressure"] = pressure
        result[f"event_{name}_pre_month"] = int(pre_overlap > 0)
        result[f"event_{name}_peak_month"] = peak_in_month
        total_pressure += pressure

    result["total_event_pressure"] = round(total_pressure, 3)
    return result


def events_in_month(events_calendar: List[Dict], year: int, month: int, zone: str, site_name: str | None = None, site_id: str | None = None) -> List[dict]:
    zone_norm = normalize_zone(zone, site_name=site_name, site_id=site_id)
    items = []
    for ev in events_calendar:
        overlap = _count_overlap_days(ev["window_start"], ev["window_end"], year, month)
        if overlap <= 0:
            continue
        name = ev["event"]
        weight = ZONE_WEIGHTS.get(name, {}).get(zone_norm, 0.15)
        items.append({
            "name": name,
            "date": ev["date"].isoformat(),
            "window_start": ev["window_start"].isoformat(),
            "window_end": ev["window_end"].isoformat(),
            "days_in_month": overlap,
            "zone_weight": weight,
            "pressure": round(overlap * weight, 3),
        })
    return items


def build_event_features(df: pd.DataFrame, year_start: int, year_end: int) -> pd.DataFrame:
    events_calendar = get_event_dates(year_start, year_end)

    for event_name in EVENTS:
        df[f"event_{event_name}_in_month"] = 0
        df[f"event_{event_name}_days"] = 0
        df[f"event_{event_name}_pressure"] = 0.0
        df[f"event_{event_name}_pre_month"] = 0
        df[f"event_{event_name}_peak_month"] = 0
    df["total_event_pressure"] = 0.0

    for idx, row in df.iterrows():
        year = int(row["year"])
        month = int(row["month"])
        features = event_features_for_month(
            events_calendar,
            year,
            month,
            zone=row.get("zone"),
            site_name=row.get("site_name"),
            site_id=row.get("site_id"),
        )
        for key, value in features.items():
            df.at[idx, key] = value

    return df