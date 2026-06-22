# ml/features/meteo.py
from __future__ import annotations

import calendar
from datetime import date
from functools import lru_cache

import requests

from ml.features.zones import normalize_zone

ZONE_COORDS = {
    "DKR": (14.693, -17.447),
    "THIES": (14.789, -16.935),
    "DIOURBEL": (14.655, -16.231),
    "LOUGA": (15.619, -16.224),
    "KAOLACK": (14.151, -16.072),
    "ZIGUINCHOR": (12.565, -16.272),
    "SAINT-LOUIS": (16.018, -16.499),
    "TAMBACOUNDA": (13.771, -13.667),
    "KOLDA": (12.898, -14.951),
    "FATICK": (14.339, -16.411),
    "MATAM": (15.656, -13.255),
    "KAFFRINE": (14.106, -15.551),
    "SEDHIOU": (12.708, -15.557),
    "KEDOUGOU": (12.555, -12.175),
}


def _safe_mean(values):
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _safe_sum(values):
    vals = [float(v) for v in values if v is not None]
    return round(sum(vals), 2) if vals else None



@lru_cache(maxsize=800)
def fetch_zone_meteo_cached(zone: str, year: int, month: int) -> dict:
    """Météo mensuelle par zone.

    Pour un mois futur : moyenne du même mois sur les 3 dernières années disponibles.
    """
    zone_norm = normalize_zone(zone)
    today = date.today()
    is_future = (year, month) > (today.year, today.month)

    if is_future:
        samples = []
        for past_year in [today.year - 1, today.year - 2, today.year - 3]:
            sample = _fetch_monthly_raw(zone_norm, past_year, month)
            if sample:
                samples.append(sample)

        if samples:
            return {
                "temp_max_mean": round(sum(s["temp_max_mean"] for s in samples) / len(samples), 2),
                "temp_min_mean": round(sum(s["temp_min_mean"] for s in samples) / len(samples), 2),
                "precip_total": round(sum(s["precip_total"] for s in samples) / len(samples), 2),
                "humidity_max": round(sum(s["humidity_max"] for s in samples) / len(samples), 2),
                "et0_mean": round(sum(s["et0_mean"] for s in samples) / len(samples), 3),
                "is_hivernage": int(month in [6, 7, 8, 9, 10]),
                "meteo_source": "historical_avg_3y",
            }
        return _default_meteo(zone_norm, month)

    result = _fetch_monthly_raw(zone_norm, year, month)
    if result:
        result["meteo_source"] = "open_meteo_archive"
        return result
    return _default_meteo(zone_norm, month)


def _fetch_monthly_raw(zone: str, year: int, month: int) -> dict | None:
    zone_norm = normalize_zone(zone)
    lat, lon = ZONE_COORDS.get(zone_norm, ZONE_COORDS["DKR"])
    last_day = calendar.monthrange(year, month)[1]

    try:
        response = requests.get(
            "https://archive-api.open-meteo.com/v1/archive",
            params={
                "latitude": lat,
                "longitude": lon,
                "start_date": f"{year}-{month:02d}-01",
                "end_date": f"{year}-{month:02d}-{last_day}",
                "daily": [
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "precipitation_sum",
                    "relative_humidity_2m_max",
                    "et0_fao_evapotranspiration",
                ],
                "timezone": "Africa/Dakar",
            },
            timeout=15,
        )
        response.raise_for_status()
        daily = response.json().get("daily", {})

        temp_max = _safe_mean(daily.get("temperature_2m_max", []))
        temp_min = _safe_mean(daily.get("temperature_2m_min", []))
        precip = _safe_sum(daily.get("precipitation_sum", []))
        humidity = _safe_mean(daily.get("relative_humidity_2m_max", []))
        et0 = _safe_mean(daily.get("et0_fao_evapotranspiration", []))

        if temp_max is None and precip is None:
            return None

        return {
            "temp_max_mean": temp_max if temp_max is not None else 32.0,
            "temp_min_mean": temp_min if temp_min is not None else 22.0,
            "precip_total": precip if precip is not None else 0.0,
            "humidity_max": humidity if humidity is not None else 65.0,
            "et0_mean": et0 if et0 is not None else 4.5,
            "is_hivernage": int(month in [6, 7, 8, 9, 10]),
        }
    except Exception:
        return None


def _default_meteo(zone: str, month: int) -> dict:
    is_hiv = month in [6, 7, 8, 9, 10]
    return {
        "temp_max_mean": 30.0 if is_hiv else 35.0,
        "temp_min_mean": 23.0 if is_hiv else 20.0,
        "precip_total": 120.0 if is_hiv else 5.0,
        "humidity_max": 85.0 if is_hiv else 55.0,
        "et0_mean": 4.5,
        "is_hivernage": int(is_hiv),
        "meteo_source": "default_senegal",
    }