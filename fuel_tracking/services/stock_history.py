"""
Évolution mensuelle du stock en cuve (sites GE du périmètre).

Stock de fin de mois d'un site = son relevé VALIDE le plus récent DATÉ DANS LE MOIS, parmi :
  - relevés terrain / gardiennage / ENOC retenus par le registre (stock_registry.fuel_stock_retained, à la fin du mois) ;
  - stock final validé de la Synthèse (Closing) ;
  - capteur Snowflake : snapshot mensuel archivé (sync_fuel_stock --month) ; pour le mois en cours, snapshot courant.
Même jour : terrain > capteur > Synthèse (même règle que services/current_stock).
Un site sans relevé dans le mois n'est pas compté (ni 0 L, ni report du mois précédent) : le nombre de sites relevés
est donné pour chaque mois, et le remplissage (stock ÷ capacité des mêmes sites) reste comparable d'un mois à l'autre.
"""
from __future__ import annotations

import calendar
import re
from collections import defaultdict
from datetime import date, datetime, time

from django.core.cache import cache
from django.utils import timezone

from fuel_tracking.services.current_stock import SOURCE_LABELS, _TIE, _day, tank_capacities

CACHE_S = 15 * 60


def _bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def last_months(n: int, today: date | None = None) -> list[str]:
    today = today or timezone.localdate()
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def month_end_stock(month: str, sites: list[dict]) -> dict[str, dict]:
    """`sites` : [{site_id, capacity_snowflake_l}] — retourne par site {stock_l, date, source, estime, capacite_l}."""
    from fuel_tracking.models import FuelStockObservation as Obs, FuelStockSnapshot
    from fuel_tracking.services.stock_registry import fuel_stock_retained

    start, end = _bounds(month)
    today = timezone.localdate()
    ids = [s["site_id"] for s in sites]
    end_dt = timezone.make_aware(datetime.combine(end, time.max))
    cands: dict[str, list] = defaultdict(list)

    for sid, f in fuel_stock_retained(ids, as_of=end_dt).items():
        if f.get("stock_l") is not None and f.get("observation_datetime") and start <= _day(f["observation_datetime"]) <= end:
            cands[sid].append((_day(f["observation_datetime"]), _TIE["TERRAIN"], float(f["stock_l"]), f["source_type"], False, f.get("capacity_l")))
    seen = set()
    for o in Obs.objects.filter(source_type="CLOSING", site_id__in=ids, validation_status="VALIDATED", level_liters__isnull=False,
                                observation_datetime__lte=end_dt).order_by("site_id", "-observation_datetime", "-id") \
            .values("site_id", "observation_datetime", "level_liters", "source_context"):
        if o["site_id"] in seen:
            continue
        seen.add(o["site_id"])
        d = _day(o["observation_datetime"])
        if start <= d <= end:
            origine = (o["source_context"] or {}).get("origine_stock")
            cands[o["site_id"]].append((d, _TIE["CLOSING"], float(o["level_liters"]), "CLOSING",
                                        bool(origine and re.search(r"estimation", str(origine), re.I)), None))
    snaps = FuelStockSnapshot.objects.filter(site_id__in=ids, stock_snowflake_l__isnull=False)
    snaps = snaps.filter(snapshot_year__isnull=True) if (start <= today <= end) else snaps.filter(snapshot_year=start.year, snapshot_month=start.month)
    for s in snaps.values("site_id", "stock_snowflake_l", "stock_snowflake_date", "capacity_snowflake_l"):
        d = s["stock_snowflake_date"]
        if d and start <= d <= end:
            cands[s["site_id"]].append((d, _TIE["SNOWFLAKE"], float(s["stock_snowflake_l"]), "SNOWFLAKE", False, None))

    caps = tank_capacities(ids)
    cap_sf = {s["site_id"]: s.get("capacity_snowflake_l") for s in sites}
    out = {}
    for sid, cs in cands.items():
        cs.sort(key=lambda x: (-x[0].toordinal(), x[1]))
        d, _t, litres, src, estime, cap_f = cs[0]
        cap = cap_f or caps.get(sid) or (float(cap_sf[sid]) if cap_sf.get(sid) else None)
        out[sid] = {"stock_l": litres, "date": d, "source": src, "estime": estime, "capacite_l": float(cap) if cap else None}
    return out


def stock_evolution(nb_months: int = 12, zone: str | None = None) -> dict:
    from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot
    from fuel_tracking.views_bi import _site_zones, scope_q

    nb_months = max(3, min(24, nb_months))
    key = f"fuel-stock-evolution:v1:{nb_months}:{timezone.localdate()}"
    base = cache.get(key)
    if base is None:
        sites = list(FuelStockSnapshot.objects.filter(scope_q(), snapshot_year__isnull=True, has_genset=True)
                     .values("site_id", "capacity_snowflake_l"))
        base = {"sites": [s["site_id"] for s in sites],
                "months": [(m, month_end_stock(m, sites)) for m in last_months(nb_months)]}
        try:
            cache.set(key, base, CACHE_S)
        except Exception:  # cache indisponible : calcul direct, l'écran ne tombe pas
            pass
    zones = _site_zones()
    keep = {s for s in base["sites"] if not zone or zones.get(s) == zone}
    current = timezone.localdate().strftime("%Y-%m")
    livre = defaultdict(float)
    for r in FuelConsommationMonthly.objects.filter(month_year__in=[m for m, _ in base["months"]], site_id__in=list(keep), enoc_nb_demandes__gt=0) \
            .values("month_year", "enoc_qte_ajoutee_l"):
        livre[r["month_year"]] += float(r["enoc_qte_ajoutee_l"] or 0)

    out = []
    for month, by_site in base["months"]:
        rows = [v for s, v in by_site.items() if s in keep]
        with_cap = [v for v in rows if v["capacite_l"]]
        src = defaultdict(int)
        for v in rows:
            src[SOURCE_LABELS.get(v["source"], v["source"])] += 1
        stock_cap = sum(v["stock_l"] for v in with_cap)
        out.append({
            "month": month, "en_cours": month == current,
            "stock_l": round(sum(v["stock_l"] for v in rows), 1) if rows else None,
            "nb_sites_releves": len(rows), "nb_sites_ge": len(keep),
            "couverture_pct": round(len(rows) / len(keep) * 100, 1) if keep else None,
            "remplissage_pct": round(stock_cap / sum(v["capacite_l"] for v in with_cap) * 100, 1) if with_cap else None,
            "nb_sites_capacite": len(with_cap),
            "nb_estimes": sum(1 for v in rows if v["estime"]),
            "sources": dict(sorted(src.items(), key=lambda kv: -kv[1])),
            "livre_enoc_l": round(livre[month], 1) if livre.get(month) else None,
        })
    return {"months": out, "zone": zone or None}
