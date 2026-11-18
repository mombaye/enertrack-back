# fuel_tracking/services/stock_initial_sources.py
"""
Tableau de Suivi Conso au format du fichier Output App Fuel : blocs « INVENTAIRE » et « DONNÉES DE STOCK INITIAL ».

Règle de date : toutes les valeurs sont prises à la DATE EXACTE du relevé initial du fichier de clôture (« Date de Relevé
Initiale »), jamais sur une autre période. Exemple : relevé initial le 30/09/2026 → on ne lit que les données du 30/09/2026.

Sources (lecture seule ; ENOC et Snowflake ne sont jamais modifiés) :
  INVENTAIRE           Gestion des sites (core.Site) + inventaire Snowflake SITE_ESCO_CURRENT (GRID_SUPPLY_MODIFIED) ;
  Date / Qtité (relevé de terrain)          fichier de clôture (Closing / Output App Fuel) ;
  Qtité (Société de gardiennage)            relevés de gardiennage ENOC (Loliforce, SCSS, ENOC_GARDIENNAGE) du même jour ;
  Qtité (Fuel Sensor FMS)                   Snowflake VW_FUEL_REPORT.LAST_VALID_LEVEL du même jour ;
  Qtité retenue                             FMS si disponible, sinon relevé de terrain ;
  Compteur genset (Société de gardiennage)  compteur horaire des relevés ENOC de gardiennage du même jour ;
  Compteur genset (Index Compteur FMS)      Snowflake GFMS_DATA_TRACKER_NC.DG_TOTAL_RUNNING_TIME_MINUTES (dernière valeur du jour ÷ 60) ;
  Compteur retenu                           FMS si disponible, sinon gardiennage.
Snowflake indisponible : les colonnes FMS restent vides (« n.d. »), jamais 0 ; l'écran ne casse pas.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from django.core.cache import cache

logger = logging.getLogger(__name__)
GUARD_SOURCES = ("ENOC_GARDIENNAGE", "LOLIFORCE", "SCSS")
SRC_TERRAIN, SRC_FMS, SRC_GUARD = "Relevé de terrain (fichier de clôture)", "Fuel Sensor FMS (Snowflake)", "Société de gardiennage (ENOC)"
SRC_INDEX_FMS = "Index Compteur FMS (Snowflake)"


def _f(v):
    return None if v is None else round(float(v), 3)


def _day(v) -> date | None:
    if not v:
        return None
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


def _guard_readings(pairs: set[tuple[str, date]]) -> dict[tuple[str, date], dict]:
    """Relevé de gardiennage ENOC le plus tardif du jour exact, par (site, jour)."""
    from django.utils import timezone

    from fuel_tracking.models import FuelStockObservation as Obs

    if not pairs:
        return {}
    sites = {s for s, _ in pairs}
    days = sorted({d for _, d in pairs})
    tz = timezone.get_current_timezone()
    lo = timezone.make_aware(datetime.combine(days[0], datetime.min.time()), tz)
    hi = timezone.make_aware(datetime.combine(days[-1] + timedelta(days=1), datetime.min.time()), tz)
    out: dict[tuple[str, date], dict] = {}
    qs = (Obs.objects.filter(source_type__in=GUARD_SOURCES, site_id__in=sites, observation_datetime__gte=lo, observation_datetime__lt=hi)
          .exclude(validation_status="REJECTED").order_by("observation_datetime")
          .values_list("site_id", "observation_datetime", "level_liters", "estimated_liters", "level_value", "level_unit", "hour_meter"))
    for sid, dt, liters, est, val, unit, hm in qs:
        key = (sid, timezone.localtime(dt, tz).date())
        if key not in pairs:
            continue
        cur = out.setdefault(key, {"litres": None, "compteur": None})
        litres = liters if liters is not None else est if est is not None else (val if unit == "L" else None)
        if litres is not None:
            cur["litres"] = _f(litres)
        if hm is not None:
            cur["compteur"] = _f(hm)
    return out


def _snowflake_points(pairs: set[tuple[str, date]]) -> tuple[dict[tuple[str, date], dict], str | None]:
    """(site, jour) → {"niveau_l", "index_h"} lus dans Snowflake au jour exact. Cache 1 h. (valeurs, message d'erreur)."""
    from fuel_tracking.models import FuelSiteInventory as INV

    if not pairs:
        return {}, None
    key = "fuel-stock-initial-sf:" + str(hash(tuple(sorted((s, d.isoformat()) for s, d in pairs))))
    hit = cache.get(key)
    if hit is not None:
        return hit
    data_ids = {sid: did for sid, did in INV.objects.filter(site_id__in={s for s, _ in pairs}).values_list("site_id", "data_id")}
    out: dict[tuple[str, date], dict] = {}
    errors = []
    try:
        from fuel_tracking.services.fuel_daily_facts_snowflake import FUEL_REPORT, PROD, _connect

        days = sorted({d for _, d in pairs})
        sites = sorted({s for s, _ in pairs})
        ph_s = ",".join(["%s"] * len(sites))
        ph_d = ",".join(["%s"] * len(days))
        conn = _connect()
    except Exception as e:  # noqa: BLE001 — Snowflake injoignable : colonnes FMS « n.d. », jamais bloquant
        logger.warning("Snowflake injoignable pour les valeurs du stock initial : %s", e)
        return {}, "Snowflake injoignable : valeurs FMS non lues."
    try:
        cur = conn.cursor()
        # 1. Niveau du capteur (Fuel Sensor FMS) — lu indépendamment du compteur
        try:
            cur.execute(f"SELECT SITE_ID, DATE, LAST_VALID_LEVEL FROM {FUEL_REPORT}.VW_FUEL_REPORT "
                        f"WHERE SITE_ID IN ({ph_s}) AND DATE IN ({ph_d}) AND LAST_VALID_LEVEL IS NOT NULL", [*sites, *days])
            for sid, d, lvl in cur.fetchall():
                k = (sid, _day(d))
                if k in pairs:
                    out.setdefault(k, {})["niveau_l"] = _f(lvl)
        except Exception as e:  # noqa: BLE001
            logger.warning("VW_FUEL_REPORT illisible (niveau FMS) : %s", e)
            errors.append("niveau Fuel Sensor FMS non lu (VW_FUEL_REPORT en erreur côté Snowflake)")
        # 2. Index compteur FMS — une vue en erreur côté Snowflake ne fait pas perdre le niveau
        ids = sorted({data_ids[s] for s in sites if s in data_ids})
        if ids:
            try:
                by_id = {did: sid for sid, did in data_ids.items()}
                ph_i = ",".join(["%s"] * len(ids))
                cur.execute(f"""
                    SELECT ID, CAST("TIMESTAMP" AS DATE) AS day, DG_TOTAL_RUNNING_TIME_MINUTES
                    FROM {PROD}.GFMS_DATA_TRACKER_NC
                    WHERE ID IN ({ph_i}) AND CAST("TIMESTAMP" AS DATE) IN ({ph_d}) AND DG_TOTAL_RUNNING_TIME_MINUTES IS NOT NULL
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY ID, CAST("TIMESTAMP" AS DATE) ORDER BY "TIMESTAMP" DESC) = 1""", [*ids, *days])
                for did, d, minutes in cur.fetchall():
                    k = (by_id.get(did), _day(d))
                    if k in pairs and minutes is not None:
                        out.setdefault(k, {})["index_h"] = round(float(minutes) / 60.0, 2)
            except Exception as e:  # noqa: BLE001
                logger.warning("GFMS_DATA_TRACKER_NC illisible (index compteur FMS) : %s", e)
                errors.append("index compteur FMS non lu (vue GFMS_DATA_TRACKER_NC en erreur côté Snowflake)")
    finally:
        conn.close()
    result = (out, ("Snowflake : " + " ; ".join(errors) + ".") if errors else None)
    cache.set(key, result, 3600)       # résultat partiel compris (une vue en erreur ne se répare pas en quelques minutes)
    return result


def enrich(rows: list[dict]) -> str | None:
    """Ajoute à chaque ligne du tableau les blocs « inventaire » et « stock_initial_sources ». Retourne un message si Snowflake a échoué."""
    from core.models import Site

    from fuel_tracking.models import FuelSiteInventory as INV

    ids = [r["site_id"] for r in rows]
    sites = {s["site_id"].upper(): s for s in Site.objects.filter(site_id__in=ids).values(
        "site_id", "zone", "site_type", "installed_site_type", "ordered_typology", "billing_typology")}
    zones = dict(Site.ZONE_CHOICES)
    grid = {sid.upper(): g for sid, g in INV.objects.filter(site_id__in=ids).values_list("site_id", "grid_supply")}
    pairs = {(r["site_id"], _day(r["stock"].get("date_initial"))) for r in rows if r["stock"].get("date_initial")}
    guards = _guard_readings(pairs)
    sf, err = _snowflake_points(pairs)
    for r in rows:
        s = sites.get(r["site_id"].upper()) or {}
        kind = s.get("installed_site_type") or s.get("site_type")
        r["inventaire"] = {
            "region": (zones.get(s.get("zone")) or "").upper() or None, "latitude": None, "longitude": None, "sla": None,
            "indoor_outdoor": {"INDOOR": "Indoor", "OUTDOOR": "Outdoor"}.get(kind, kind),
            "on_grid": grid.get(r["site_id"].upper()), "typologie_cible": s.get("ordered_typology"), "typologie_facturee": s.get("billing_typology"),
        }
        d = _day(r["stock"].get("date_initial"))
        terrain = r["stock"].get("initial_l") if (r["stock"].get("source_initial") or "").lower() == "closing" else None
        g = guards.get((r["site_id"], d), {}) if d else {}
        p = sf.get((r["site_id"], d), {}) if d else {}
        fms, idx = p.get("niveau_l"), p.get("index_h")
        retenue, src = (fms, SRC_FMS) if fms is not None else ((terrain, SRC_TERRAIN) if terrain is not None else (None, None))
        cpt, csrc = (idx, SRC_INDEX_FMS) if idx is not None else ((g.get("compteur"), SRC_GUARD) if g.get("compteur") is not None else (None, None))
        r["stock_initial_sources"] = {
            "date": d.isoformat() if d else None, "terrain_l": terrain, "gardiennage_l": g.get("litres"), "fms_l": fms,
            "retenue_l": retenue, "source": src, "compteur_gardiennage_h": g.get("compteur"), "compteur_fms_h": idx,
            "compteur_retenu_h": cpt, "source_compteur": csrc,
        }
    return err
