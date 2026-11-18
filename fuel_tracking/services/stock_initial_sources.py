# fuel_tracking/services/stock_initial_sources.py
"""
Tableau de Suivi Conso au format du fichier Output App Fuel : blocs « INVENTAIRE » et « DONNÉES DE STOCK INITIAL ».

Règle de date : toutes les valeurs sont prises à la DATE EXACTE du relevé initial du fichier de clôture (« Date de Relevé
Initiale »), jamais sur une autre période. Exemple : relevé initial le 30/09/2026 → on ne lit que les données du 30/09/2026.

Sources (lecture seule ; ENOC et Snowflake ne sont jamais modifiés) :
  INVENTAIRE           Gestion des sites (core.Site) + inventaire Snowflake SITE_ESCO_CURRENT (GRID_SUPPLY_MODIFIED) ;
  Date / Qtité (relevé de terrain)          fichier de clôture (Closing / Output App Fuel) ;
  Qtité (Société de gardiennage)            ENOC, partie « Relevé de gardien » (collection fuel_level_readings), même site et même
                                            jour, valeur telle qu'enregistrée ; aucun relevé ce jour-là → vide (jamais d'estimation,
                                            jamais un fichier Loliforce/SCSS, jamais un autre jour) ;
  Qtité (Fuel Sensor FMS)                   Snowflake VW_FUEL_REPORT.LAST_VALID_LEVEL du même jour ;
  Qtité retenue                             FMS si disponible, sinon relevé de terrain ;
  Compteur genset (Société de gardiennage)  compteur horaire porté par ce même relevé de gardien ENOC, s'il existe ;
  Compteur genset (Index Compteur FMS)      Snowflake GENSET_REPORT.TOTAL_DG_RUNNING_HOUR (compteur horaire cumulé du jour) ;
  Compteur retenu                           FMS si disponible, sinon gardiennage.
Snowflake indisponible : les colonnes FMS restent vides (« n.d. »), jamais 0 ; l'écran ne casse pas.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from django.core.cache import cache

logger = logging.getLogger(__name__)
SRC_TERRAIN, SRC_FMS, SRC_GUARD = "Relevé de terrain (fichier de clôture)", "Fuel Sensor FMS (Snowflake)", "Relevé de gardien (ENOC)"
SRC_INDEX_FMS = "Index Compteur FMS (Snowflake)"


def _f(v):
    return None if v is None else round(float(v), 3)


def _day(v) -> date | None:
    if not v:
        return None
    return v if isinstance(v, date) and not isinstance(v, datetime) else date.fromisoformat(str(v)[:10])


# Champs « compteur horaire » possibles sur un relevé de gardien ENOC (lus seulement s'ils sont présents).
_HOUR_FIELDS = ("hour_meter", "compteur_horaire", "compteur", "genset_hours", "running_hours")


def _enoc_day(v) -> date | None:
    """Jour du relevé ENOC (date_releve : texte ISO ou datetime). Fuseau métier Africa/Dakar = UTC+0."""
    if not v:
        return None
    try:
        return v.date() if isinstance(v, datetime) else date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _parse_guard_doc(doc) -> dict:
    """Valeurs d'un relevé de gardien ENOC, telles qu'enregistrées. Rien n'est estimé ni converti."""
    litres = []
    for t in doc.get("tanks") or []:
        lit = _num(t.get("level_liters"))
        # 0 L sans hauteur en cm : valeur par défaut de l'import ENOC, pas une mesure → ignorée
        if lit is not None and not (lit == 0 and t.get("level_cm") is None):
            litres.append(lit)
    hours = next((_num(doc.get(k)) for k in _HOUR_FIELDS if _num(doc.get(k)) is not None), None)
    return {"litres": _f(sum(litres)) if litres else None, "compteur": _f(hours)}


def _guard_readings(pairs: set[tuple[str, date]]) -> tuple[dict[tuple[str, date], dict], str | None]:
    """ENOC « Relevé de gardien » : pour chaque (site, jour) demandé, le relevé de CE site fait CE jour-là, sinon rien.

    Lecture seule de la collection ENOC fuel_level_readings. Plusieurs relevés le même jour : le plus tardif.
    ENOC injoignable : colonnes vides et message, jamais de valeur reprise d'une autre source.
    """
    import hashlib

    if not pairs:
        return {}, None
    raw = "|".join(f"{s}@{d.isoformat()}" for s, d in sorted(pairs))
    key = "fuel-stock-initial-guard:v1:" + hashlib.sha1(raw.encode()).hexdigest()
    hit = cache.get(key)
    if isinstance(hit, tuple) and len(hit) == 2:
        return hit
    from django.conf import settings

    from fuel_tracking.services.enoc_mongo_service import _connect

    sites = sorted({s for s, _ in pairs})
    days = sorted({d for _, d in pairs})
    lo, hi = days[0], days[-1] + timedelta(days=1)
    try:
        client = _connect()
    except Exception as e:  # noqa: BLE001 — ENOC injoignable : colonne vide, jamais bloquant
        logger.warning("ENOC injoignable pour les relevés de gardien : %s", e)
        result = ({}, "ENOC injoignable : relevés de gardien non lus.")
        cache.set(key, result, 300)   # évite 8 s d'attente à chaque page tant qu'ENOC est injoignable
        return result
    out: dict[tuple[str, date], dict] = {}
    try:
        db = client[settings.ENOC_MONGO_DB_NAME]
        crit = {"site_id": {"$in": sites}, "$or": [
            {"date_releve": {"$gte": lo.isoformat(), "$lt": hi.isoformat()}},                                    # texte ISO
            {"date_releve": {"$gte": datetime.combine(lo, datetime.min.time()), "$lt": datetime.combine(hi, datetime.min.time())}},
        ]}
        best: dict[tuple[str, date], tuple[str, dict]] = {}
        for doc in db.fuel_level_readings.find(crit):
            k = (doc.get("site_id"), _enoc_day(doc.get("date_releve")))
            if k not in pairs:
                continue
            stamp = str(doc.get("date_releve"))
            if k not in best or stamp >= best[k][0]:
                best[k] = (stamp, doc)
        for k, (_, doc) in best.items():
            v = _parse_guard_doc(doc)
            if v["litres"] is not None or v["compteur"] is not None:
                out[k] = v
        result = (out, None)
    except Exception as e:  # noqa: BLE001
        logger.warning("Lecture ENOC fuel_level_readings impossible : %s", e)
        result = ({}, "ENOC : relevés de gardien non lus (erreur de lecture).")
    finally:
        client.close()
    cache.set(key, result, 3600)
    return result


def _snowflake_points(pairs: set[tuple[str, date]]) -> tuple[dict[tuple[str, date], dict], str | None]:
    """(site, jour) → {"niveau_l", "index_h"} lus dans Snowflake au jour exact. Cache 1 h. (valeurs, message d'erreur)."""
    from fuel_tracking.models import FuelSiteInventory as INV

    if not pairs:
        return {}, None
    import hashlib

    # clé stable entre processus (hash() de Python change à chaque démarrage : le cache n'était jamais partagé)
    raw = "|".join(f"{s}@{d.isoformat()}" for s, d in sorted(pairs))
    key = "fuel-stock-initial-sf:v3:" + hashlib.sha1(raw.encode()).hexdigest()
    hit = cache.get(key)
    if isinstance(hit, tuple) and len(hit) == 2:
        return hit
    data_ids = {sid: did for sid, did in INV.objects.filter(site_id__in={s for s, _ in pairs}).values_list("site_id", "data_id")}
    out: dict[tuple[str, date], dict] = {}
    errors = []
    try:
        from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, FUEL_REPORT, _connect

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
        # 2. Index compteur FMS : GENSET_REPORT.TOTAL_DG_RUNNING_HOUR (compteur horaire cumulé du GE, par site et par jour).
        #    Remplace la vue GFMS_DATA_TRACKER_NC, cassée côté Snowflake ; lu indépendamment du niveau.
        ids = sorted({data_ids[s] for s in sites if s in data_ids})
        if ids:
            try:
                by_id = {did: sid for sid, did in data_ids.items()}
                ph_i = ",".join(["%s"] * len(ids))
                cur.execute(f"SELECT DATA_ID, DATE, TOTAL_DG_RUNNING_HOUR FROM {ANALYTICS}.GENSET_REPORT "
                            f"WHERE DATA_ID IN ({ph_i}) AND DATE IN ({ph_d}) AND TOTAL_DG_RUNNING_HOUR IS NOT NULL", [*ids, *days])
                for did, d, hours in cur.fetchall():
                    k = (by_id.get(int(did)) if str(did).isdigit() else by_id.get(did), _day(d))
                    if k in pairs and hours is not None:
                        out.setdefault(k, {})["index_h"] = round(float(hours), 2)
            except Exception as e:  # noqa: BLE001
                logger.warning("GENSET_REPORT illisible (index compteur FMS) : %s", e)
                errors.append("index compteur FMS non lu (GENSET_REPORT en erreur côté Snowflake)")
    finally:
        conn.close()
    result = (out, ("Snowflake : " + " ; ".join(errors) + ".") if errors else None)
    cache.set(key, result, 6 * 3600)   # valeurs passées (dates de relevés) : stables ; résultat partiel compris
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
    guards, gerr = _guard_readings(pairs)
    sf, err = _snowflake_points(pairs)
    err = " ".join(m for m in (err, gerr) if m) or None
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
