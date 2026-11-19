# fuel_tracking/services/ge_info.py
"""
Fabricant et type de GE par site — bloc « ESTIMATION CONSOMMATION FUEL THÉORIQUE » du Suivi Conso.

Ordre, CHAMP PAR CHAMP (fabricant et type choisis indépendamment), jamais de déduction :
  1. Snowflake DB_GFMS_ANALYTICS_PROD.GOLD.SITE_DG (fiche actuelle) : VENDOR, GENSET_TYPE ;
  2. Snowflake SITE_DG_DAILY (historique) : dernière version non vide du même GE (S_ID, NOM) ;
  3. ENOC, registre des GE (ge_assets) : brand, model — GE au statut INSTALLED uniquement.
« Source de donnée GE » indique la source exacte (une seule si les deux champs viennent du même endroit).
Rattachement Snowflake : S_ID = identifiant interne du site → SITE_ESCO_CURRENT.ID → SITE_ID (jointure dans Snowflake ;
S_ID n'est PAS le DATA_ID). Plusieurs GE sur un site : valeurs distinctes jointes par « / » dans l'ordre des GE.

Puissance PRP (kVA) : feuille « GENSET DB » du dernier fichier de clôture importé (copie d'archive), colonne « PRP [kVA] » de
la ligne dont le « Type de GE » correspond EXACTEMENT au type du site (casse, espaces et ponctuation ignorés ; « P33-3U » ≠
« P33-3 »). Type présent plusieurs fois avec des PRP différentes : ambigu, aucune valeur. Puissance PRP (kW) = kVA × 0,8.

Lecture seule, faite EN TÂCHE DE FOND (refresh), cache 48 h relu toutes les 6 h au plus ; la page lit le cache uniquement.
Garde-fou : si moins de MIN_MATCH des S_ID de SITE_DG trouvent un site unique, Snowflake n'est pas utilisé (raison
conservée) ; un S_ID rattaché à plusieurs SITE_ID n'est jamais utilisé. Une source en échec n'empêche pas les autres.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
CACHE_KEY = "fuel-ge-info:v5"   # v5 : + puissance PRP (GENSET DB)
KW_PER_KVA = 0.8                 # Puissance PRP (kW) = Puissance PRP (kVA) × 0,8 (constante fixe)
CACHE_TTL = 48 * 3600
REFRESH_AFTER_S = 6 * 3600
MIN_MATCH = 0.5
SRC_SF, SRC_SF_HIST, SRC_ENOC = "Snowflake (SITE_DG)", "Snowflake (historique SITE_DG)", "ENOC (registre des GE)"
EMPTY = {"", "NONE", "NULL", "N/A", "NA", "0", "-", "UNDEFINED", "NAN"}


def _clean(v) -> str | None:
    s = " ".join(str(v).split()) if v is not None else ""
    return None if s.upper() in EMPTY else s


def _join(values) -> str | None:
    out = list(dict.fromkeys(v for v in values if v))
    return " / ".join(out) or None


SF_SQL = """
WITH cur AS (
    SELECT S_ID, NOM, VENDOR, GENSET_TYPE FROM {a}.SITE_DG WHERE S_ID IS NOT NULL
),
hist AS (   -- dernière version non vide de chaque GE, pour les GE sans fiche actuelle ou à fiche incomplète
    SELECT S_ID, NOM, VENDOR, GENSET_TYPE FROM {a}.SITE_DG_DAILY
    WHERE S_ID IS NOT NULL AND (VENDOR IS NOT NULL OR GENSET_TYPE IS NOT NULL)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY S_ID, NOM ORDER BY AS_OF_DATE DESC) = 1
),
ge AS (
    SELECT COALESCE(c.S_ID, h.S_ID) AS s_id, COALESCE(c.NOM, h.NOM) AS nom,
           c.VENDOR AS vendor, c.GENSET_TYPE AS gtype, h.VENDOR AS h_vendor, h.GENSET_TYPE AS h_gtype
    FROM cur c FULL OUTER JOIN hist h ON h.S_ID = c.S_ID AND h.NOM = c.NOM
    -- GE seulement dans l'historique : retenu uniquement si le site n'a plus aucune fiche actuelle (jamais un GE retiré
    -- ajouté à côté des GE actuels du site)
    WHERE c.S_ID IS NOT NULL OR h.S_ID NOT IN (SELECT S_ID FROM cur)
)
SELECT g.s_id, g.nom, g.vendor, g.gtype, g.h_vendor, g.h_gtype,
       IFF(MIN(e.SITE_ID) = MAX(e.SITE_ID), MAX(e.SITE_ID), NULL) AS site_id, COUNT(DISTINCT e.SITE_ID) AS n_sites
FROM ge g LEFT JOIN {a}.SITE_ESCO_CURRENT e ON e.ID = g.s_id
GROUP BY 1, 2, 3, 4, 5, 6
"""


def fetch_snowflake() -> tuple[dict, dict]:
    """site → [(n° GE, (fabricant, source), (type, source))] depuis SITE_DG puis son historique ; (valeurs, méta)."""
    from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, _connect

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(SF_SQL.format(a=ANALYTICS))
        rows = cur.fetchall()
    finally:
        conn.close()
    ids = {int(r[0]) for r in rows}
    matched = {int(r[0]) for r in rows if r[6] and r[7] == 1}   # S_ID rattaché à plusieurs sites : jamais utilisé
    meta = {"lignes": len(rows), "s_id": len(ids), "rapproches": len(matched), "taux": round(len(matched) / len(ids), 3) if ids else 0,
            "jointure": "SITE_DG.S_ID = SITE_ESCO_CURRENT.ID"}
    out: dict = {}
    if meta["taux"] < MIN_MATCH:
        meta["ignore"] = f"S_ID rattachés à un site pour {meta['taux']:.0%} seulement : Snowflake non utilisé."
        return out, meta
    for s_id, nom, vendor, gtype, h_vendor, h_gtype, site_id, _n in rows:
        if int(s_id) not in matched:
            continue
        fab = (_clean(vendor), SRC_SF) if _clean(vendor) else ((_clean(h_vendor), SRC_SF_HIST) if _clean(h_vendor) else (None, None))
        typ = (_clean(gtype), SRC_SF) if _clean(gtype) else ((_clean(h_gtype), SRC_SF_HIST) if _clean(h_gtype) else (None, None))
        out.setdefault(str(site_id).strip().upper(), []).append((nom or 0, fab, typ))
    return out, meta


def fetch_enoc() -> dict:
    """site → [(fabricant, type)] des GE installés du registre ENOC (lecture seule)."""
    from django.conf import settings

    from fuel_tracking.services.enoc_mongo_service import _connect

    client = _connect()
    try:
        out: dict = {}
        for d in client[settings.ENOC_MONGO_DB_NAME].ge_assets.find({"asset_status": "INSTALLED"},
                                                                    {"_id": 0, "site_id": 1, "current_site_id": 1, "brand": 1, "model": 1}):
            sid = d.get("current_site_id") or d.get("site_id")
            if sid:
                out.setdefault(str(sid).strip().upper(), []).append((_clean(d.get("brand")), _clean(d.get("model"))))
        return out
    finally:
        client.close()


def _pick(snowflake_values: list, enoc_values: list) -> tuple[str | None, str | None]:
    """(valeur, source) : Snowflake (actuel puis historique, GE par GE) ; sinon ENOC ; sinon (None, None)."""
    sf = [(v, s) for v, s in snowflake_values if v]
    if sf:
        srcs = list(dict.fromkeys(s for _, s in sf))
        return _join(v for v, _ in sf), (srcs[0] if len(srcs) == 1 else " + ".join(srcs))
    en = _join(enoc_values)
    return (en, SRC_ENOC) if en else (None, None)


def build(sf: dict, enoc: dict) -> dict:
    """site → {"fabricant_ge", "type_ge", "source_ge"}."""
    out = {}
    for sid in set(sf) | set(enoc):
        ges = sorted(sf.get(sid, []), key=lambda g: g[0])
        en = enoc.get(sid, [])
        fab, fab_src = _pick([g[1] for g in ges], [_clean(e[0]) for e in en])
        typ, typ_src = _pick([g[2] for g in ges], [_clean(e[1]) for e in en])
        if not fab and not typ:
            continue
        srcs = [s for s in (fab_src, typ_src) if s]
        source = srcs[0] if len(set(srcs)) == 1 else f"Fabricant : {fab_src or 'n.d.'} · Type : {typ_src or 'n.d.'}"
        out[sid.upper()] = {"fabricant_ge": fab, "type_ge": typ, "source_ge": source}
    return out


def refresh() -> dict:
    """Relit Snowflake puis ENOC (lecture seule) et met le résultat en cache. Une source en échec n'empêche pas l'autre."""
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
    if not sf and not en and cached():
        return {"erreurs": errors, "cache": "conservé (aucune source lisible)"}
    sites = build(sf, en)
    try:
        gdb, gdb_meta = fetch_genset_db()
    except Exception as e:  # noqa: BLE001 — feuille illisible : puissances « n.d. », le reste est conservé
        logger.warning("GENSET DB illisible : %s", e)
        gdb, gdb_meta = {}, {"fichier": None}
        errors.append("GENSET DB (fichier de clôture) non lue.")
    add_prp(sites, gdb, gdb_meta.get("fichier"))
    data = {"sites": sites, "meta": {**meta, "sites_enoc": len(en), "genset_db": gdb_meta, "erreurs": errors},
            "lu_le": timezone.now().isoformat()}
    cache.set(CACHE_KEY, data, CACHE_TTL)
    par_source = {}
    for v in sites.values():
        par_source[v["source_ge"]] = par_source.get(v["source_ge"], 0) + 1
    return {"sites": len(sites), "par_source": par_source, **data["meta"]}


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


# ── Puissance PRP depuis la feuille « GENSET DB » du fichier de clôture ──
def _key(t) -> str:
    import re

    return re.sub(r"[^A-Z0-9]", "", str(t or "").upper())


def parse_genset_db(rows) -> dict:
    """Clé du « Type de GE » → ensemble des PRP [kVA] de la feuille (une seule valeur attendue)."""
    out: dict = {}
    head = None
    for row in rows:
        vals = list(row or [])
        norm = [" ".join(str(v).split()).lower() if v is not None else "" for v in vals]
        if head is None:
            if "type de ge" in norm and any(h.startswith("prp [kva]") for h in norm):
                head = (norm.index("type de ge"), next(i for i, h in enumerate(norm) if h.startswith("prp [kva]")))
            continue
        t = vals[head[0]] if head[0] < len(vals) else None
        p = vals[head[1]] if head[1] < len(vals) else None
        if _clean(t) and isinstance(p, (int, float)) and p > 0:
            out.setdefault(_key(t), set()).add(round(float(p), 2))
    return out


def fetch_genset_db() -> tuple[dict, dict]:
    """GENSET DB du fichier de clôture le plus récent qui la contient (copie d'archive, lecture seule)."""
    from pathlib import Path

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.stock_import_files import iter_sheets

    for b in (FuelImportBatch.objects.filter(source_type="CLOSING").exclude(status__in=("REJECTED", "FAILED"))
              .exclude(archive_path=None).order_by("-received_at")):
        if not Path(b.archive_path).is_file():
            continue
        gdb = {}
        try:
            for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={"GENSET DB"}):
                gdb = parse_genset_db(list(rows))   # lignes lues pendant que le classeur est ouvert
        except Exception:  # noqa: BLE001 — archive illisible : fichier précédent
            continue
        if gdb:
            return gdb, {"fichier": b.file_name, "types": len(gdb), "ambigus": sorted(k for k, v in gdb.items() if len(v) > 1)}
    return {}, {"fichier": None, "types": 0}


def add_prp(sites: dict, gdb: dict, fichier: str | None) -> None:
    """Ajoute à chaque site la Puissance PRP (kVA / kW) de chacun de ses types de GE, avec la raison si absente."""
    for v in sites.values():
        kva, motifs = [], []
        for t in [x for x in (v.get("type_ge") or "").split(" / ") if x]:
            vals = gdb.get(_key(t))
            if not vals:
                motifs.append(f"type « {t} » absent de GENSET DB" if fichier else "GENSET DB non disponible")
            elif len(vals) > 1:
                motifs.append(f"type « {t} » ambigu dans GENSET DB ({' / '.join(f'{x:g}' for x in sorted(vals))} kVA)")
            else:
                kva.append(next(iter(vals)))
        v["prp_kva"] = [round(x, 2) for x in kva] or None
        v["prp_kw"] = [round(x * KW_PER_KVA, 2) for x in kva] or None
        v["prp_motif"] = " ; ".join(motifs) or None
        v["prp_source"] = f"GENSET DB ({fichier})" if kva and fichier else None
