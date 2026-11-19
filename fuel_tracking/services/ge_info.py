# fuel_tracking/services/ge_info.py
"""
Fabricant et type de GE par site — bloc « ESTIMATION CONSOMMATION FUEL THÉORIQUE » du Suivi Conso.

Règle (par champ) : Snowflake d'abord, sinon ENOC ; jamais de déduction (le fabricant n'est pas deviné depuis le code du type).
  Snowflake  DB_GFMS_ANALYTICS_PROD.GOLD.SITE_DG : VENDOR (fabricant), GENSET_TYPE (type), une ligne par GE (NOM = n° du GE),
             S_ID = identifiant numérique du site, rapproché du DATA_ID de l'inventaire Snowflake (FuelSiteInventory).
  ENOC       registre des GE (collection ge_assets) : brand, model — GE au statut INSTALLED uniquement.
Plusieurs GE sur un site : valeurs distinctes jointes par « / » dans l'ordre des GE.

Lecture seule des deux sources, faite EN TÂCHE DE FOND (refresh) et gardée en cache 48 h ; la page lit le cache uniquement.
Garde-fou : si moins de MIN_MATCH des S_ID de SITE_DG correspondent à un DATA_ID de l'inventaire, Snowflake n'est pas utilisé
(rapprochement non fiable) et la raison est conservée dans le résultat.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
CACHE_KEY = "fuel-ge-info:v1"
CACHE_TTL = 48 * 3600
REFRESH_AFTER_S = 6 * 3600
MIN_MATCH = 0.5
SRC_SF, SRC_ENOC = "Snowflake (SITE_DG)", "ENOC (registre des GE)"
EMPTY = {"", "NONE", "NULL", "N/A", "NA", "0", "-", "UNDEFINED", "NAN"}


def _clean(v) -> str | None:
    s = " ".join(str(v).split()) if v is not None else ""
    return None if s.upper() in EMPTY else s


def _join(values) -> str | None:
    out = list(dict.fromkeys(v for v in values if v))
    return " / ".join(out) or None


def fetch_snowflake() -> tuple[dict, dict]:
    """site → [(n° GE, fabricant, type)] lus dans SITE_DG ; (valeurs, méta de rapprochement)."""
    from fuel_tracking.models import FuelSiteInventory as INV
    from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, _connect

    inv = {}
    for did, sid in INV.objects.values_list("data_id", "site_id"):
        inv.setdefault(int(did), set()).add(sid)
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT S_ID, NOM, VENDOR, GENSET_TYPE FROM {ANALYTICS}.SITE_DG WHERE S_ID IS NOT NULL")
        rows = cur.fetchall()
    finally:
        conn.close()
    ids = {int(r[0]) for r in rows}
    matched = {i for i in ids if len(inv.get(i, ())) == 1}   # DATA_ID ambigu (plusieurs sites) : jamais rattaché
    meta = {"lignes": len(rows), "s_id": len(ids), "rapproches": len(matched), "taux": round(len(matched) / len(ids), 3) if ids else 0}
    out: dict = {}
    if meta["taux"] < MIN_MATCH:
        meta["ignore"] = f"S_ID rapprochés du DATA_ID pour {meta['taux']:.0%} seulement : Snowflake non utilisé."
        return out, meta
    for s_id, nom, vendor, gtype in rows:
        if int(s_id) in matched:
            out.setdefault(next(iter(inv[int(s_id)])), []).append((nom or 0, _clean(vendor), _clean(gtype)))
    return out, meta


def fetch_enoc() -> dict:
    """site → [(fabricant, type)] des GE installés du registre ENOC."""
    from django.conf import settings

    from fuel_tracking.services.enoc_mongo_service import _connect

    client = _connect()
    try:
        out: dict = {}
        for d in client[settings.ENOC_MONGO_DB_NAME].ge_assets.find({"asset_status": "INSTALLED"},
                                                                    {"_id": 0, "site_id": 1, "current_site_id": 1, "brand": 1, "model": 1}):
            sid = d.get("current_site_id") or d.get("site_id")
            if sid:
                out.setdefault(str(sid).strip(), []).append((_clean(d.get("brand")), _clean(d.get("model"))))
        return out
    finally:
        client.close()


def build(sf: dict, enoc: dict) -> dict:
    """site → {"fabricant_ge", "type_ge", "source_ge"} selon la règle Snowflake d'abord, sinon ENOC (par champ)."""
    out = {}
    for sid in set(sf) | set(enoc):
        ges = sorted(sf.get(sid, []), key=lambda g: g[0])
        sf_fab, sf_type = _join(g[1] for g in ges), _join(g[2] for g in ges)
        en_fab, en_type = _join(g[0] for g in enoc.get(sid, [])), _join(g[1] for g in enoc.get(sid, []))
        fab, fab_src = (sf_fab, SRC_SF) if sf_fab else ((en_fab, SRC_ENOC) if en_fab else (None, None))
        typ, typ_src = (sf_type, SRC_SF) if sf_type else ((en_type, SRC_ENOC) if en_type else (None, None))
        if not fab and not typ:
            continue
        srcs = {s for s in (fab_src, typ_src) if s}
        source = next(iter(srcs)) if len(srcs) == 1 else f"Fabricant : {fab_src} · Type : {typ_src}"
        out[sid.upper()] = {"fabricant_ge": fab, "type_ge": typ, "source_ge": source}
    return out


def refresh() -> dict:
    """Relit Snowflake et ENOC (lecture seule) et met le résultat en cache. Une source en échec n'empêche pas l'autre."""
    meta, errors = {}, []
    try:
        sf, meta = fetch_snowflake()
        if meta.get("ignore"):
            errors.append(meta["ignore"])
    except Exception as e:  # noqa: BLE001
        logger.warning("SITE_DG illisible (fabricant / type de GE) : %s", e)
        sf, errors = {}, errors + ["Snowflake SITE_DG non lu."]
    try:
        en = fetch_enoc()
    except Exception as e:  # noqa: BLE001
        logger.warning("ENOC ge_assets illisible : %s", e)
        en, errors = {}, errors + ["ENOC (registre des GE) non lu."]
    data = {"sites": build(sf, en), "meta": {**meta, "sites_enoc": len(en), "erreurs": errors}, "lu_le": timezone.now().isoformat()}
    cache.set(CACHE_KEY, data, CACHE_TTL)
    return {"sites": len(data["sites"]), **data["meta"]}


def cached() -> dict | None:
    try:
        return cache.get(CACHE_KEY)
    except Exception:  # noqa: BLE001
        return None


def is_stale(data: dict | None) -> bool:
    if not data:
        return True
    try:
        from datetime import datetime

        return (timezone.now() - datetime.fromisoformat(data["lu_le"])).total_seconds() > REFRESH_AFTER_S
    except Exception:  # noqa: BLE001
        return True


def request_refresh() -> None:
    """Lance la lecture en tâche de fond (une seule à la fois)."""
    try:
        if cache.add(CACHE_KEY + ":lock", 1, 300):
            from fuel_tracking.tasks import refresh_ge_info

            refresh_ge_info.delay()
    except Exception as e:  # noqa: BLE001 — pas de file Celery : rien n'est bloqué, la case reste « n.d. »
        logger.warning("Lecture du fabricant / type de GE non lancée : %s", e)
