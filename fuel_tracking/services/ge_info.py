# fuel_tracking/services/ge_info.py
"""
Fabricant et type de GE par site — bloc « ESTIMATION CONSOMMATION FUEL THÉORIQUE » du Suivi Conso.

Source unique : Snowflake DB_GFMS_ANALYTICS_PROD.GOLD.SITE_DG (lecture seule) — VENDOR (fabricant), GENSET_TYPE (type),
une ligne par GE (NOM = n° du GE). S_ID = identifiant interne du site, rattaché au SITE_ID par l'inventaire Snowflake
SITE_ESCO_CURRENT (colonne ID) — jointure faite dans Snowflake. (S_ID n'est PAS le DATA_ID : vérifié en prod, 4 % seulement.)
Valeur absente dans Snowflake → « n.d. » (jamais déduite ni reprise d'une autre source).
Plusieurs GE sur un site : valeurs distinctes jointes par « / » dans l'ordre des GE.

Lecture faite EN TÂCHE DE FOND (refresh) et gardée en cache 48 h, relue toutes les 6 h au plus ; la page lit le cache uniquement.
Garde-fou : si moins de MIN_MATCH des S_ID de SITE_DG trouvent un site unique dans SITE_ESCO_CURRENT, rien n'est rattaché
(rapprochement non fiable) et la raison est conservée ; un S_ID rattaché à plusieurs SITE_ID n'est jamais utilisé.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
CACHE_KEY = "fuel-ge-info:v3"   # v3 : rattachement par SITE_ESCO_CURRENT.ID
CACHE_TTL = 48 * 3600
REFRESH_AFTER_S = 6 * 3600
MIN_MATCH = 0.5
SRC_SF = "Snowflake (SITE_DG)"
EMPTY = {"", "NONE", "NULL", "N/A", "NA", "0", "-", "UNDEFINED", "NAN"}


def _clean(v) -> str | None:
    s = " ".join(str(v).split()) if v is not None else ""
    return None if s.upper() in EMPTY else s


def _join(values) -> str | None:
    out = list(dict.fromkeys(v for v in values if v))
    return " / ".join(out) or None


def fetch_snowflake() -> tuple[dict, dict]:
    """site → [(n° GE, fabricant, type)] lus dans SITE_DG, rattachés au SITE_ID par SITE_ESCO_CURRENT.ID ; (valeurs, méta)."""
    from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, _connect

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"""
            SELECT d.S_ID, d.NOM, d.VENDOR, d.GENSET_TYPE,
                   IFF(MIN(e.SITE_ID) = MAX(e.SITE_ID), MAX(e.SITE_ID), NULL) AS site_id,
                   COUNT(DISTINCT e.SITE_ID) AS n_sites
            FROM {ANALYTICS}.SITE_DG d
            LEFT JOIN {ANALYTICS}.SITE_ESCO_CURRENT e ON e.ID = d.S_ID
            WHERE d.S_ID IS NOT NULL
            GROUP BY d.S_ID, d.NOM, d.VENDOR, d.GENSET_TYPE
        """)
        rows = cur.fetchall()
    finally:
        conn.close()
    ids = {int(r[0]) for r in rows}
    matched = {int(r[0]) for r in rows if r[4] and r[5] == 1}   # S_ID rattaché à plusieurs sites : jamais utilisé
    meta = {"lignes": len(rows), "s_id": len(ids), "rapproches": len(matched), "taux": round(len(matched) / len(ids), 3) if ids else 0,
            "jointure": "SITE_DG.S_ID = SITE_ESCO_CURRENT.ID"}
    out: dict = {}
    if meta["taux"] < MIN_MATCH:
        meta["ignore"] = f"S_ID rattachés à un site pour {meta['taux']:.0%} seulement : fabricant / type de GE non rattachés."
        return out, meta
    for s_id, nom, vendor, gtype, site_id, _n in rows:
        if int(s_id) in matched:
            out.setdefault(str(site_id).strip(), []).append((nom or 0, _clean(vendor), _clean(gtype)))
    return out, meta


def build(sf: dict) -> dict:
    """site → {"fabricant_ge", "type_ge", "source_ge"} depuis Snowflake SITE_DG."""
    out = {}
    for sid, ges in sf.items():
        ges = sorted(ges, key=lambda g: g[0])
        fab, typ = _join(_clean(g[1]) for g in ges), _join(_clean(g[2]) for g in ges)
        if fab or typ:
            out[sid.upper()] = {"fabricant_ge": fab, "type_ge": typ, "source_ge": SRC_SF}
    return out


def refresh() -> dict:
    """Relit SITE_DG (lecture seule) et met le résultat en cache. Snowflake injoignable : l'ancien cache est conservé."""
    try:
        sf, meta = fetch_snowflake()
    except Exception as e:  # noqa: BLE001
        logger.warning("SITE_DG illisible (fabricant / type de GE) : %s", e)
        return {"erreur": "Snowflake SITE_DG non lu."}
    data = {"sites": build(sf), "meta": meta, "lu_le": timezone.now().isoformat()}
    cache.set(CACHE_KEY, data, CACHE_TTL)
    return {"sites": len(data["sites"]), **meta}


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
