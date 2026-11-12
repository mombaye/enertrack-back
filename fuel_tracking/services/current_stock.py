"""
Stock actuel d'un site : le relevé LE PLUS RÉCENT parmi toutes les sources disponibles.

  - relevés terrain / gardiennage / ENOC : stock retenu par le registre (stock_registry.fuel_stock_retained :
    cuve validée, litres validés, capacité contrôlée) ;
  - capteur Snowflake : dernier niveau du snapshot stock ;
  - Synthèse (Closing mensuel) : stock final VALIDÉ importé par la Configuration.

Le jour le plus récent l'emporte. Même jour : relevé terrain > capteur > Synthèse (la Synthèse est un document de
clôture, souvent recopié d'un relevé ou estimé). Un stock de Synthèse noté « Estimation » est marqué estimé.
Un relevé en cm non converti, rejeté ou à valider n'est jamais retenu (aucune valeur inventée). Un stock capteur
sans date n'est retenu qu'à défaut de tout relevé daté (âge inconnu → signalé « à confirmer » dans le plan).
"""
from __future__ import annotations

import re

from django.utils import timezone

SOURCE_LABELS = {
    "SNOWFLAKE": "Capteur", "CLOSING": "Synthèse", "SCSS": "SCSS", "LOLIFORCE": "Loliforce",
    "ENOC": "ENOC", "TERRAIN_MENSUEL": "Relevé terrain",
}
_TIE = {"TERRAIN": 0, "SNOWFLAKE": 1, "CLOSING": 2}


def _day(dt):
    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


def latest_closing(site_ids) -> dict[str, dict]:
    """Dernier stock VALIDÉ de la Synthèse par site (litres natifs)."""
    from fuel_tracking.models import FuelStockObservation as Obs

    out: dict[str, dict] = {}
    qs = Obs.objects.filter(source_type="CLOSING", site_id__in=list(site_ids), validation_status="VALIDATED", level_liters__isnull=False) \
        .order_by("site_id", "-observation_datetime", "-id").values("site_id", "observation_datetime", "level_liters", "source_context")
    for o in qs:
        out.setdefault(o["site_id"], o)
    return out


def tank_capacities(site_ids) -> dict[str, float]:
    """Capacité totale des cuves VALIDÉES du site, seulement si toutes ont une capacité connue."""
    from fuel_tracking.services.stock_registry import site_tanks

    out = {}
    for sid, tanks in site_tanks(site_ids).items():
        caps = [t.check_capacity_l() for t in tanks if t.validated]
        if caps and all(caps):
            out[sid] = round(sum(caps), 1)
    return out


def current_stock(snapshots: list[dict]) -> dict[str, dict]:
    """
    `snapshots` : lignes du snapshot stock (site_id, stock_snowflake_l, stock_snowflake_date, capacity_snowflake_l).
    Retourne par site : stock_l, date, source, source_label, estime, origine, capacite_l, capacite_source.
    """
    from fuel_tracking.services.stock_registry import fuel_stock_retained

    site_ids = [s["site_id"] for s in snapshots]
    if not site_ids:
        return {}
    field = fuel_stock_retained(site_ids)
    closing = latest_closing(site_ids)
    caps = tank_capacities(site_ids)

    out = {}
    for s in snapshots:
        sid = s["site_id"]
        cands = []
        f = field.get(sid)
        if f and f.get("stock_l") is not None and f.get("observation_datetime"):
            cands.append((_day(f["observation_datetime"]), _TIE["TERRAIN"], {
                "stock_l": float(f["stock_l"]), "source": f["source_type"], "estime": False, "origine": f.get("statut_releve")}))
        if s.get("stock_snowflake_l") is not None:  # sans date : retenu seulement à défaut de tout relevé daté
            cands.append((s["stock_snowflake_date"], _TIE["SNOWFLAKE"], {
                "stock_l": float(s["stock_snowflake_l"]), "source": "SNOWFLAKE", "estime": False, "origine": None}))
        c = closing.get(sid)
        if c:
            origine = (c.get("source_context") or {}).get("origine_stock")
            cands.append((_day(c["observation_datetime"]), _TIE["CLOSING"], {
                "stock_l": float(c["level_liters"]), "source": "CLOSING",
                "estime": bool(origine and re.search(r"estimation", str(origine), re.I)), "origine": origine}))
        cap, cap_src = None, None
        if f and f.get("capacity_l"):
            cap, cap_src = float(f["capacity_l"]), "Cuves validées"
        elif sid in caps:
            cap, cap_src = caps[sid], "Cuves validées"
        elif s.get("capacity_snowflake_l"):
            cap, cap_src = float(s["capacity_snowflake_l"]), "Capteur"
        if not cands:
            out[sid] = {"stock_l": None, "date": None, "source": None, "source_label": None, "estime": False, "origine": None,
                        "capacite_l": cap, "capacite_source": cap_src, "nb_sources": 0}
            continue
        cands.sort(key=lambda x: (x[0] is None, -(x[0].toordinal() if x[0] else 0), x[1]))
        d, _t, best = cands[0]
        out[sid] = {**best, "date": d, "source_label": SOURCE_LABELS.get(best["source"], best["source"]),
                    "capacite_l": cap, "capacite_source": cap_src, "nb_sources": len(cands)}
    return out
