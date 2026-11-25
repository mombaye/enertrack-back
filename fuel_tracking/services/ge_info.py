# fuel_tracking/services/ge_info.py
"""
Fabricant et type de GE par site — bloc « ESTIMATION CONSOMMATION FUEL THÉORIQUE » du Suivi Conso.

Ordre, CHAMP PAR CHAMP (fabricant et type choisis indépendamment), jamais de déduction :
  Fabricant GE : 1. fichier « Base GE » : colonne « Fabricant GE » si le fichier l'a (Base GE VF5), sinon marque lue dans
                 le libellé « Type de GE » (« SDMO - K22 » → SDMO ;
                 « FG Wilson P33-3U » → FG Wilson, marque connue) ; 2. Snowflake SITE_DG (VENDOR) puis historique ; 3. ENOC (brand).
  Type de GE   : 1. fichier « Base GE » importé dans Configuration (type BASE_GE, dernier fichier) — décision métier ;
                 2. à défaut (décision du 24/11/2026) : fichier de clôture DU MOIS AFFICHÉ, sur la ligne du site (Closing :
                    « Type de GE #1 / #2 » ; Output App Fuel : « Type de GE ») — le fabricant de la même ligne l'accompagne ;
                 3. à défaut : ENOC (model), puis Snowflake SITE_DG / historique ; source affichée.
  ENOC : registre des GE (ge_assets), GE au statut INSTALLED uniquement.
« Source de donnée GE » indique la source exacte (une seule si les deux champs viennent du même endroit).
Rattachement Snowflake : S_ID = identifiant interne du site → SITE_ESCO_CURRENT.ID → SITE_ID (jointure dans Snowflake ;
S_ID n'est PAS le DATA_ID). Plusieurs GE sur un site : valeurs distinctes jointes par « / » dans l'ordre des GE.

Puissance PRP (kVA) et courbe CPH : D'ABORD l'onglet « GENSET DB » du dernier fichier Base GE (VF5, décision du 25/11/2026) pour
les types qu'il contient ; sinon feuille « GENSET DB » du Closing de référence (FUEL_GENSET_DB_MONTH, défaut 2026-09 = Closing de
septembre ; à défaut le dernier fichier de clôture qui la contient), copie d'archive, colonne « PRP [kVA] » de
la ligne dont le « Type de GE » correspond EXACTEMENT au type du site (casse, espaces et ponctuation ignorés ; « P33-3U » ≠
« P33-3 »). Type présent plusieurs fois avec des PRP différentes : ambigu, aucune valeur. Puissance PRP (kW) = kVA × 0,8.

Lecture seule, faite EN TÂCHE DE FOND (refresh), cache 48 h relu toutes les 6 h au plus ; la page lit le cache uniquement.
Garde-fou : si moins de MIN_MATCH des S_ID de SITE_DG trouvent un site unique, Snowflake n'est pas utilisé (raison
conservée) ; un S_ID rattaché à plusieurs SITE_ID n'est jamais utilisé. Une source en échec n'empêche pas les autres.
"""
from __future__ import annotations

import logging
import re

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)
CACHE_KEY = "fuel-ge-info:v9"   # v9 : données brutes en cache ; Base GE lue en direct à chaque affichage
KW_PER_KVA = 0.8                 # Puissance PRP (kW) = Puissance PRP (kVA) × 0,8 (constante fixe)
CACHE_TTL = 48 * 3600
REFRESH_AFTER_S = 6 * 3600
MIN_MATCH = 0.5
SRC_BASE = "Fichier Base GE"
SRC_SF, SRC_SF_HIST, SRC_ENOC = "Snowflake (SITE_DG)", "Snowflake (historique SITE_DG)", "ENOC (registre des GE)"
SRC_CLOSING = "Fichier de clôture"
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


def _pick_enoc_first(snowflake_values: list, enoc_values: list) -> tuple[str | None, str | None]:
    """(valeur, source) : ENOC d'abord ; sinon Snowflake (actuel puis historique) ; sinon (None, None)."""
    en = _join(enoc_values)
    return (en, SRC_ENOC) if en else _pick(snowflake_values, [])


def _pick(snowflake_values: list, enoc_values: list) -> tuple[str | None, str | None]:
    """(valeur, source) : Snowflake (actuel puis historique, GE par GE) ; sinon ENOC ; sinon (None, None)."""
    sf = [(v, s) for v, s in snowflake_values if v]
    if sf:
        srcs = list(dict.fromkeys(s for _, s in sf))
        return _join(v for v, _ in sf), (srcs[0] if len(srcs) == 1 else " + ".join(srcs))
    en = _join(enoc_values)
    return (en, SRC_ENOC) if en else (None, None)


def build(sf: dict, enoc: dict, base: dict | None = None, base_file: str | None = None, brands=(),
          closing: dict | None = None, base_fab: dict | None = None) -> dict:
    """site → {"fabricant_ge", "type_ge", "source_ge"}.
    `base` : site → (type, kVA) du fichier Base GE (1ʳᵉ source) ; `closing` : site → (fabricant, type, source affichée) lus sur
    la ligne du fichier de clôture du mois affiché (2ᵉ source) ; puis ENOC / Snowflake."""
    base, closing, base_fab = base or {}, closing or {}, base_fab or {}
    out = {}
    for sid in set(sf) | set(enoc) | set(base) | set(closing):
        ges = sorted(sf.get(sid, []), key=lambda g: g[0])
        en = enoc.get(sid, [])
        fab, fab_src = _pick([g[1] for g in ges], [_clean(e[0]) for e in en])
        typ, typ_src = _pick_enoc_first([g[2] for g in ges], [_clean(e[1]) for e in en])   # 3. ENOC puis Snowflake
        c_fab, c_typ, c_src = (list(closing.get(sid) or ()) + [None, None, None])[:3]
        if _clean(c_typ):   # 2. fichier de clôture du mois
            typ, typ_src = _clean(c_typ), c_src or SRC_CLOSING
            if _clean(c_fab):
                fab, fab_src = _clean(c_fab), c_src or SRC_CLOSING
        if _clean((base.get(sid) or (None,))[0]):   # 1. type : fichier Base GE d'abord (décision métier)
            typ, typ_src = _clean(base[sid][0]), f"{SRC_BASE} ({base_file})" if base_file else SRC_BASE
            from fuel_tracking.services.base_ge import split_label

            marque, _modele = split_label(typ, brands)
            if _clean(base_fab.get(sid)):   # colonne « Fabricant GE » du fichier Base GE (VF5) d'abord
                marque = base_fab[sid]
            if _clean(marque):   # fabricant : marque écrite dans le libellé du fichier Base GE d'abord
                fab, fab_src = _clean(marque), typ_src
        if not fab and not typ:
            continue
        srcs = [s for s in (fab_src, typ_src) if s]
        source = srcs[0] if len(set(srcs)) == 1 else f"Fabricant : {fab_src or 'n.d.'} · Type : {typ_src or 'n.d.'}"
        out[sid.upper()] = {"fabricant_ge": fab, "type_ge": typ, "source_ge": source}
    return out


def source_family(source: str | None) -> str | None:
    """Famille de la source du TYPE de GE (filtre de l'écran) : le libellé complet porte le nom du fichier et la période."""
    s = str(source or "")
    if s.startswith("Fabricant :") and "Type :" in s:
        s = s.split("Type :", 1)[1].strip()
    if not s or s.startswith("n.d."):
        return None
    if s.startswith(SRC_BASE):
        return SRC_BASE
    if s.startswith(SRC_CLOSING):
        return f"{SRC_CLOSING} (type Base GE non reconnu)" if "type Base GE" in s else SRC_CLOSING
    if "ENOC" in s:
        return SRC_ENOC
    if "Snowflake" in s:
        return "Snowflake (SITE_DG)"
    return s


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
    from fuel_tracking.services.base_ge import latest_types

    base, base_file = latest_types()
    if not sf and not en and not base and cached():
        return {"erreurs": errors, "cache": "conservé (aucune source lisible)"}
    try:
        gdb, gdb_meta = fetch_genset_db()
    except Exception as e:  # noqa: BLE001 — feuille illisible : puissances « n.d. », le reste est conservé
        logger.warning("GENSET DB illisible : %s", e)
        gdb, gdb_meta = {}, {"fichier": None}
        errors.append("GENSET DB (fichier de clôture) non lue.")
    if not gdb:
        if gdb_meta.get("archives_introuvables"):
            errors.append(f"GENSET DB : {gdb_meta['archives_introuvables']} copie(s) d'archive Closing introuvable(s) "
                          "(registre non monté dans ce conteneur ?).")
        elif gdb_meta.get("lots_closing"):
            errors.append("GENSET DB : aucune feuille « GENSET DB » exploitable dans les fichiers de clôture.")
        prev = (cached() or {}).get("raw") or {}
        if prev.get("gdb"):   # dernière lecture réussie conservée : une archive momentanément inaccessible n'efface pas la PRP
            gdb, gdb_meta = prev["gdb"], {**(prev.get("gdb_meta") or {}), "conservee": True, "courbes": prev.get("gdb_curves") or {}}
    curves = gdb_meta.pop("courbes", None) or {}   # courbes CPH en cache à part (le résumé « meta » reste lisible)
    gdb_meta["courbes_cph"] = len(curves)
    from fuel_tracking.services.base_ge import known_brands

    m = {"fabricants": gdb_meta.get("fabricants") or []}
    gdb_all, curves_all, bsrc = with_base_genset_db(gdb, curves, m)   # onglet GENSET DB du fichier Base GE d'abord
    brands = known_brands([t for t, _k in base.values()], m["fabricants"])
    from fuel_tracking.services.base_ge import latest_fabricants

    sites = build(sf, en, base, base_file, brands, base_fab=latest_fabricants())
    add_prp(sites, gdb_all, gdb_meta.get("fichier"), curves_all, bsrc)
    data = {"sites": sites, "raw": {"sf": sf, "enoc": en, "gdb": gdb, "gdb_meta": gdb_meta, "gdb_curves": curves},
            "meta": {**meta, "sites_enoc": len(en), "base_ge": {"fichier": base_file, "sites": len(base)},
                                     "genset_db": gdb_meta, "erreurs": errors},
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


def parse_genset_db(rows, manufacturers: set | None = None, curves: dict | None = None) -> dict:
    """Clé du « Type de GE » → ensemble des PRP [kVA] de la feuille (une seule valeur attendue).
    `curves` (facultatif) reçoit clé → ensemble des coefficients (a, b, c) de la courbe CPH (L/h) = a·x² + b·x + c, x = taux
    de charge en fraction : colonnes a, b, c de la feuille ; absentes → ajustement des points Conso 50 / 75 / 100 %."""
    out: dict = {}
    head = None
    for row in rows:
        vals = list(row or [])
        norm = [" ".join(str(v).split()).lower() if v is not None else "" for v in vals]
        if head is None:
            if "type de ge" in norm and any(h.startswith("prp [kva]") for h in norm):
                head = (norm.index("type de ge"), next(i for i, h in enumerate(norm) if h.startswith("prp [kva]")),
                        next((i for i, h in enumerate(norm) if h.startswith("manufacturer")), None))
                cols = {k: (norm.index(k) if k in norm else None) for k in ("a", "b", "c")}
                cols.update({k: next((i for i, h in enumerate(norm) if h.startswith(f"conso {k}")), None) for k in ("50", "75", "100")})
            continue
        t = vals[head[0]] if head[0] < len(vals) else None
        p = vals[head[1]] if head[1] < len(vals) else None
        mf = vals[head[2]] if head[2] is not None and head[2] < len(vals) else None
        if _clean(t) and isinstance(p, (int, float)) and p > 0:
            if manufacturers is not None and _clean(mf):
                manufacturers.add(" ".join(str(mf).split()))
            abc = _curve(vals, cols) if curves is not None else None
            for k in {_key(t), _key(f"{mf or ''}{t}")}:   # « K22 » et « SDMOK22 » (libellé « SDMO - K22 »)
                out.setdefault(k, set()).add(round(float(p), 2))
                if abc:
                    curves.setdefault(k, set()).add(abc)
    return out


def _curve(vals, cols) -> tuple | None:
    """(a, b, c) d'une ligne GENSET DB : colonnes a, b, c ; sinon ajustement exact des points Conso 50 / 75 / 100 % (L/h)."""
    num = lambda k: (vals[cols[k]] if cols.get(k) is not None and cols[k] < len(vals) and isinstance(vals[cols[k]], (int, float))  # noqa: E731
                     and not isinstance(vals[cols[k]], bool) else None)
    a, b, c = num("a"), num("b"), num("c")
    if None in (a, b, c):
        y50, y75, y100 = num("50"), num("75"), num("100")
        if None in (y50, y75, y100):
            return None
        a = (y100 - 2 * y75 + y50) * 8
        b = (y100 - y50) * 2 - a * 1.5
        c = y100 - a - b
    return (round(float(a), 6), round(float(b), 6), round(float(c), 6))


def fetch_genset_db() -> tuple[dict, dict]:
    """GENSET DB du fichier de clôture le plus récent qui la contient (copie d'archive, lecture seule)."""
    from pathlib import Path

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.stock_import_files import iter_sheets

    from django.conf import settings

    from fuel_tracking.services.stock_period_balance import reference_batches

    ref = [x for x in reference_batches(getattr(settings, "FUEL_GENSET_DB_MONTH", "2026-09"))]
    qs = list(FuelImportBatch.objects.filter(source_type="CLOSING").exclude(status__in=("REJECTED", "FAILED"))
              .exclude(archive_path=None).order_by("-received_at"))
    absents, illisibles = [], []
    for b in sorted(qs, key=lambda x: (x.pk not in ref, ref.index(x.pk) if x.pk in ref else 0)):   # Closing de référence d'abord
        if not Path(b.archive_path).is_file():
            absents.append(b.file_name)   # registre non monté dans ce conteneur, ou copie d'archive supprimée
            continue
        gdb, mfs, crv = {}, set(), {}
        try:
            for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={"GENSET DB"}):
                gdb = parse_genset_db(list(rows), mfs, crv)   # lignes lues pendant que le classeur est ouvert
        except Exception as e:  # noqa: BLE001 — archive illisible : fichier précédent
            logger.warning("GENSET DB : archive %s illisible : %s", b.file_name, e)
            illisibles.append(b.file_name)
            continue
        if gdb:
            return gdb, {"fichier": b.file_name, "types": len(gdb), "ambigus": sorted(k for k, v in gdb.items() if len(v) > 1),
                         "fabricants": sorted(mfs), "courbes": crv}
    if absents:
        logger.warning("GENSET DB : %d copie(s) d'archive Closing introuvable(s) (ex. %s) — registre %s monté dans ce "
                       "conteneur ?", len(absents), absents[0], getattr(settings, "FUEL_IMPORT_ROOT", "?"))
    return {}, {"fichier": None, "types": 0, "lots_closing": len(qs), "archives_introuvables": len(absents),
                "archives_illisibles": len(illisibles)}


# Correspondance EXPLICITE type du site → type GENSET DB, utilisée seulement si le type n'est pas trouvé tel quel. Clés et
# valeurs = modèle normalisé (_key, sans la marque). Validée le 24/11/2026 (liste A : même modèle, seul un suffixe ou une
# faute de frappe diffère, PRP cohérente avec le nom). Jamais de rapprochement approché ni de révision différente sans
# validation de l'Energy Desk (ex. P33-3U, GEP22-4 : en attente). Affichée dans la source de la PRP.
GENSET_ALIASES = {
    "J66": "J66K",          # SDMO J66 → J66K (60 kVA)
    "J165K": "J165",        # SDMO J165K → J165 (165 kVA)
    "J88K": "J88",          # SDMO J88K → J88 (88 kVA)
    "J110": "J110K",        # SDMO J110 → J110K (100 kVA)
    "V410K": "V410",        # SDMO V410K → V410 (410 kVA)
    "V275C2": "V275",       # SDMO V275C2 → V275 (275 kVA)
    "GBW15P": "GBW15",      # PRAMAC GBW15P → GBW15 (15,3 kVA)
    "DE200DE0": "DE200E0",  # CATERPILLAR DE200DE0 → DE200E0 (180 kVA), faute de frappe
    "TJ22PE": "TJ22PE5L",   # TEKSAN TJ22PE → TJ22PE5L (20 kVA)
}


# Fiches constructeur (fichier « Correspondance complète NEWTABLE_SITE_DG_FIXED / Abaque CPH Sénégal », onglet « Fiches
# constructeur », vérifiées le 24/11/2026) : PRP et courbe CPH des types ABSENTS de GENSET DB, utilisées seulement quand GENSET
# DB ne donne rien pour le type. Coefficients a, b, c du fichier (points 100 / 75 / 50 % du PRP). Le T66K (FC-01, équivalence
# provisoire J66K à confirmer sur plaque) n'est PAS repris. Décision du 25/11/2026.
FICHES_CONSTRUCTEUR = (
    # (id, fabricant, modèle, PRP kVA, (a, b, c), validation)
    ("FC-02", "Grupel", "G0014PKSTCA5T4000A", 12.5, (0.8, 2.2, 0.7), "fiche du moteur Perkins 403A-15G1"),
    ("FC-03", "Green Power", "GP16 SH/PW-A", 13.1, (0.8, 2.2, 0.7), "fiche du moteur Perkins 403A-15G1"),
    ("FC-04", "GreenPole", "LG16P", 15.0, (2.4, 0.6, 1.3), "fiche du moteur Perkins 403A-15G2"),
    ("FC-05", "Olympian", "GEP18-4", 16.5, (1.6, 1.2, 1.6), "fiche du groupe (18 kVA = secours)"),
    ("FC-06", "Jet Generators", "JP20", 20.0, (1.6, 2.4, 1.3), "fiche du moteur"),
    ("FC-07", "FG Wilson", "P33-3U", 30.0, (2.4, 2.6, 1.9), "fiche FG Wilson du P33-3U"),
)
FICHE_BY_KEY = {k: f for f in FICHES_CONSTRUCTEUR for k in {_key(f[2]), _key(f[1] + f[2])}}


def _hit(label: str, table: dict) -> tuple[str | None, str | None]:
    """(clé de la table retenue pour ce type, correspondance appliquée « X → Y » ou None). Type tel quel d'abord, puis la
    correspondance explicite GENSET_ALIASES du modèle."""
    keys = _lookup_keys(label)
    k = next((k for k in keys if k in table), None)
    if k is not None:
        return k, None
    for k in keys:
        alias = GENSET_ALIASES.get(k)
        if alias and alias in table:
            return alias, f"{k} → {alias}"
    return None, None


def _resolve(label: str, table: dict) -> tuple[object, str | None]:
    """(valeur de la table pour ce type, correspondance appliquée « X → Y » ou None)."""
    k, corr = _hit(label, table)
    return (table[k], corr) if k is not None else (None, None)


def with_base_genset_db(gdb: dict, curves: dict | None, meta: dict | None = None) -> tuple[dict, dict, tuple | None]:
    """Décision du 25/11/2026 : PRP et courbe CPH lues D'ABORD dans l'onglet « GENSET DB » du dernier fichier Base GE (VF5).
    Un type présent dans cet onglet y prend PRP ET courbe (jamais une PRP d'un fichier avec la courbe d'un autre) ; les autres
    types gardent la GENSET DB du Closing de référence, puis les fiches constructeur. → (gdb, courbes, (clés, fichier) ou None)."""
    from fuel_tracking.services.base_ge import latest_genset_db

    bg, bc, bf, bfile = latest_genset_db()
    if not bg:
        return gdb or {}, curves or {}, None
    g = {k: v for k, v in (gdb or {}).items() if k not in bg}
    g.update(bg)
    c = {k: v for k, v in (curves or {}).items() if k not in bg}
    c.update(bc)
    if meta is not None:
        meta["fabricants"] = sorted(set(meta.get("fabricants") or []) | set(bf))
    return g, c, (set(bg), bfile)


def add_prp(sites: dict, gdb: dict, fichier: str | None, curves: dict | None = None, base: tuple | None = None) -> None:
    """Ajoute à chaque site la Puissance PRP (kVA / kW) de chacun de ses types de GE, avec la raison si absente.
    GENSET DB d'abord (onglet du fichier Base GE `base` = (clés, fichier) prioritaire, sinon Closing) ; type absent →
    fiche constructeur validée (FICHES_CONSTRUCTEUR) si elle existe."""
    base_keys, base_file = base or (set(), None)
    any_db = fichier or base_file
    for v in sites.values():
        kva, motifs, abc, via, fiches, files = [], [], [], [], [], []
        for t in [x for x in (v.get("type_ge") or "").split(" / ") if x]:
            key, corr = _hit(t, gdb)
            vals = gdb[key] if key is not None else None
            cv = _resolve(t, curves)[0] if curves else None
            if vals and len(vals) == 1:
                files.append(base_file if key in base_keys else fichier)
            if not vals:
                f = next((FICHE_BY_KEY[k] for k in _lookup_keys(t) if k in FICHE_BY_KEY), None)
                if f:
                    vals, cv, corr = {f[3]}, {f[4]}, None
                    fiches.append(f"fiche constructeur {f[0]} ({f[1]} {f[2]}, {f[5]})")
            if corr and vals:
                via.append(corr)
            abc.append(next(iter(cv)) if cv and len(cv) == 1 else None)   # plusieurs courbes pour un type : ambigu, aucune
            if not vals:
                motifs.append(f"type « {t} » absent de GENSET DB" if any_db else "GENSET DB non disponible")
            elif len(vals) > 1:
                motifs.append(f"type « {t} » ambigu dans GENSET DB ({' / '.join(f'{x:g}' for x in sorted(vals))} kVA)")
            else:
                kva.append(next(iter(vals)))
        v["prp_kva"] = [round(x, 2) for x in kva] or None
        v["prp_kw"] = [round(x * KW_PER_KVA, 2) for x in kva] or None
        v["prp_motif"] = " ; ".join(motifs) or None
        uniq = list(dict.fromkeys(f for f in files if f))
        parts = [f"GENSET DB ({f})" for f in uniq] + fiches
        if parts and via and uniq:
            parts[len(uniq) - 1] += f" — correspondance de type validée : {', '.join(via)}"
        v["prp_source"] = " ; ".join(parts) if kva and parts else None
        v["courbes_cph"] = abc or None   # (a, b, c) par GE, dans l'ordre des types ; None = courbe absente ou ambiguë


def _lookup_keys(label: str) -> list[str]:
    """Clés GENSET DB d'un libellé de type : tel quel (« K22 » ou « SDMO - K22 » = fabricant + type), puis le modèle seul
    après le séparateur « MARQUE - MODÈLE » (tiret entouré d'espaces, ou double tiret « SDMO--J66 »). Jamais approché."""
    keys = [_key(label)]
    parts = re.split(r"\s+-+\s*|\s*-{2,}\s*", str(label or "").strip(), maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        keys.append(_key(parts[1]))
    return keys


def sites_info(site_ids, closing: dict | None = None) -> dict:
    """site → fabricant / type / source / PRP, calculés À L'AFFICHAGE pour ces sites : fichier Base GE lu en base (toujours le
    dernier importé, sans attendre de relecture), Snowflake / ENOC / GENSET DB repris du cache (relus en tâche de fond)."""
    from fuel_tracking.services.base_ge import known_brands, latest_types

    ids = {str(s).upper() for s in site_ids}
    data = cached() or {}
    raw = data.get("raw") or {}
    sf = {k: v for k, v in (raw.get("sf") or {}).items() if k in ids}
    en = {k: v for k, v in (raw.get("enoc") or {}).items() if k in ids}
    base_all, base_file = latest_types()
    base = {k: v for k, v in base_all.items() if k in ids}
    gdb_meta = raw.get("gdb_meta") or {}
    m = {"fabricants": gdb_meta.get("fabricants") or []}
    # PRP / courbes : onglet GENSET DB du dernier fichier Base GE (lu en direct, comme les types), puis GENSET DB du Closing
    gdb, curves, bsrc = with_base_genset_db(raw.get("gdb") or {}, raw.get("gdb_curves"), m)
    fichier = gdb_meta.get("fichier")
    brands = known_brands([t for t, _k in base_all.values()], m["fabricants"])
    closing = {k.upper(): v for k, v in (closing or {}).items() if k.upper() in ids}
    from fuel_tracking.services.base_ge import latest_fabricants

    sites = build(sf, en, base, base_file, brands, closing, latest_fabricants())
    add_prp(sites, gdb, fichier, curves, bsrc)
    closing_if_base_unknown(sites, base, closing, gdb, fichier, curves, bsrc)
    return sites


def closing_if_base_unknown(sites: dict, base: dict, closing: dict, gdb: dict, fichier, curves, bsrc: tuple | None = None) -> int:
    """Décision du 24/11/2026 : type du fichier Base GE sans aucune PRP dans GENSET DB (absent ou ambigu) → type de la ligne du
    fichier de clôture du mois, s'il en donne une. La source garde la trace du type Base GE écarté et de la raison."""
    n = 0
    for sid, v in sites.items():
        c_fab, c_typ, c_src = (list(closing.get(sid) or ()) + [None, None, None])[:3]
        if sid not in base or v.get("prp_kva") or not _clean(c_typ) or _clean(c_typ) == v.get("type_ge"):
            continue
        alt = {"type_ge": _clean(c_typ)}
        add_prp({sid: alt}, gdb, fichier, curves, bsrc)
        if not alt.get("prp_kva"):
            continue   # le fichier de clôture ne fait pas mieux : type Base GE conservé (avec son motif)
        note = f"type Base GE « {v.get('type_ge')} » : {v.get('prp_motif') or 'PRP n.d.'}"
        alt.update(fabricant_ge=_clean(c_fab) or v.get("fabricant_ge"),
                   source_ge=f"{c_src or SRC_CLOSING} — {note}", type_ge_base=v.get("type_ge"))
        v.update(alt)
        n += 1
    return n
