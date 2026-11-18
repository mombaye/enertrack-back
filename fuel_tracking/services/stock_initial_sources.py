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

Bloc « DONNÉES DE RAVITAILLEMENT » (6 colonnes) : TOUT vient d'ENOC (lecture seule), sur la plage de dates de la ligne du
fichier de chaque site : « Date de Relevé Initiale » → « Date de Relevé Finale » (même règle de bornes que le bilan :
après le jour du relevé initial à 00:00, jusqu'au jour du relevé final à 00:00 inclus).
  Total Livraison Fuel EYDON                    livraisons ENOC intégrées, hors Carte Total ;
  Total Livraison Fuel CARTE TOTAL              livraisons ENOC Carte Total intégrées ;
  Total Ajout                                   ponctions entrantes ENOC (rajouts d'un site à l'autre) ;
  Total Retrait                                 ponctions sortantes ENOC ;
  Total QTE volée déclarée                      vols déclarés dans ENOC ;
  Total QTE vidangée / Fuite / Écart de Stock   vidanges déclarées dans ENOC.
Mouvement rejeté, non intégré, ponction déséquilibrée : non compté. Aucune ligne de fichier (pas de dates) : vide, jamais 0.
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


def _fetch_guard(pairs: set[tuple[str, date]]) -> tuple[dict[tuple[str, date], dict], str | None]:
    """ENOC « Relevé de gardien » : pour chaque (site, jour) demandé, le relevé de CE site fait CE jour-là, sinon rien.

    Lecture seule de la collection ENOC fuel_level_readings. Plusieurs relevés le même jour : le plus tardif.
    ENOC injoignable : colonnes vides et message, jamais de valeur reprise d'une autre source.
    """
    if not pairs:
        return {}, None
    from django.conf import settings

    from fuel_tracking.services.enoc_mongo_service import _connect

    sites = sorted({s for s, _ in pairs})
    days = sorted({d for _, d in pairs})
    lo, hi = days[0], days[-1] + timedelta(days=1)
    try:
        client = _connect()
    except Exception as e:  # noqa: BLE001 — ENOC injoignable : colonne vide, jamais bloquant
        logger.warning("ENOC injoignable pour les relevés de gardien : %s", e)
        return {}, "ENOC injoignable : relevés de gardien non lus."
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
    return result


def _fetch_snowflake(pairs: set[tuple[str, date]]) -> tuple[dict[tuple[str, date], dict], str | None]:
    """(site, jour) → {"niveau_l", "index_h"} lus dans Snowflake au jour exact (lecture seule). (valeurs, message d'erreur)."""
    from fuel_tracking.models import FuelSiteInventory as INV

    if not pairs:
        return {}, None
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
        # Requête bornée dans le temps (paramètre de SESSION, aucune écriture) : jamais de requête qui bloque indéfiniment.
        try:
            cur.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 120")
        except Exception:  # noqa: BLE001
            pass
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
    return out, (("Snowflake : " + " ; ".join(errors) + ".") if errors else None)


# ── Cache par (site, jour) ────────────────────────────────────────────────────────────────────────────────────────────────
# Chaque valeur lue (Snowflake FMS, ENOC gardien) est gardée par site et par jour de relevé : changer de page, passer à 200
# par page ou à « Tous les sites » réutilise ce qui a déjà été lu. Les dates de relevé sont passées : valeurs stables (24 h).
# Échec de lecture : gardé 10 min seulement, avec son message, pour réessayer ensuite sans bloquer chaque affichage.
PAIR_TTL_OK, PAIR_TTL_ERR, FETCH_CHUNK = 24 * 3600, 600, 150
PREFIX_SF, PREFIX_GUARD = "fuel-si:sf:v4", "fuel-si:guard:v2"


def _pkey(prefix: str, site: str, day: date) -> str:
    return f"{prefix}:{site}:{day.isoformat()}"


def _cached(prefix: str, pairs) -> tuple[dict, set]:
    keys = {_pkey(prefix, s, d): (s, d) for s, d in pairs}
    try:
        got = cache.get_many(list(keys)) if keys else {}
    except Exception:  # noqa: BLE001 — cache indisponible : on relit les sources
        got = {}
    found = {keys[k]: v for k, v in got.items() if isinstance(v, dict)}
    return found, set(pairs) - set(found)


def _points(prefix: str, fetch_fn, pairs, fetch: bool = True):
    """(valeurs par (site, jour), message d'erreur, paires encore non lues). Ne relit la source que pour les paires absentes du cache."""
    found, missing = _cached(prefix, pairs)
    if missing and fetch:
        todo = sorted(missing)
        for i in range(0, len(todo), FETCH_CHUNK):
            chunk = set(todo[i:i + FETCH_CHUNK])
            out, err = fetch_fn(chunk)
            vals = {pr: ({**out.get(pr, {}), "_err": err} if err else out.get(pr, {})) for pr in chunk}
            try:
                cache.set_many({_pkey(prefix, *pr): v for pr, v in vals.items()}, PAIR_TTL_ERR if err else PAIR_TTL_OK)
            except Exception:  # noqa: BLE001
                pass
            found.update(vals)
        missing = set()
    errs = sorted({v["_err"] for v in found.values() if v.get("_err")})
    return {k: {x: y for x, y in v.items() if x != "_err"} for k, v in found.items()}, (" ".join(errs) or None), missing


def _snowflake_points(pairs, fetch: bool = True):
    """(site, jour) → {"niveau_l", "index_h"} : cache par paire, Snowflake relu seulement pour les paires manquantes."""
    vals, err, _ = _points(PREFIX_SF, _fetch_snowflake, pairs, fetch)
    return vals, err


def _guard_readings(pairs, fetch: bool = True):
    """(site, jour) → relevé de gardien ENOC du jour exact : cache par paire, ENOC relu seulement pour les paires manquantes."""
    vals, err, _ = _points(PREFIX_GUARD, _fetch_guard, pairs, fetch)
    return vals, err


def row_pairs(rows) -> set:
    return {(r["site_id"], _day(r["stock"].get("date_initial"))) for r in rows if r["stock"].get("date_initial")}


def missing_pairs(pairs) -> set:
    """Paires (site, jour) dont une valeur Snowflake ou ENOC n'est pas encore en cache."""
    return _cached(PREFIX_SF, pairs)[1] | _cached(PREFIX_GUARD, pairs)[1]


def warm_pairs(pairs) -> int:
    """Lit (et met en cache) Snowflake et ENOC pour les paires manquantes. Retourne le nombre de paires lues."""
    miss = missing_pairs(pairs)
    if miss:
        _snowflake_points(miss)
        _guard_readings(miss)
    return len(miss)


RAV_KEYS = ("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l")
ENOC_MOVE_KEY = {"TRANSFERT_INTERSITE_ENTRANT": "ajout_l", "TRANSFERT_INTERSITE_SORTANT": "retrait_l", "THEFT": "vole_l", "DRAIN": "fuite_l"}


def _file_line_context(ini: dict | None, fin: dict | None) -> dict | None:
    """Contexte de la ligne du fichier de clôture retenue par le bilan (stock final, sinon initial)."""
    for x in (fin, ini):
        if isinstance(x, dict) and x.get("source") == "CLOSING" and isinstance(x.get("contexte_source"), dict):
            return x["contexte_source"]
    return None


def _file_dates(r: dict, details: dict) -> tuple[date | None, date | None]:
    """Date de Relevé Initiale / Finale de la ligne du fichier du site (sinon dates des relevés retenus par le bilan)."""
    ctx = _file_line_context(*details.get(r["stock"].get("bilan_id"), (None, None)))
    per = (ctx or {}).get("periode") or [None, None]
    return _day(per[0] or r["stock"].get("date_initial")), _day(per[1] or r["stock"].get("date_final"))


def ravitaillement(rows: list[dict]) -> None:
    """Ajoute à chaque ligne le bloc « ravitaillement » : 6 totaux lus dans ENOC sur la plage de dates du fichier du site."""
    from django.utils import timezone

    from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelMovement as MV, FuelStockBalance as B
    from fuel_tracking.services.stock_enoc import check_transfer_balance

    ids = [r["stock"].get("bilan_id") for r in rows if r["stock"].get("bilan_id")]
    details = {pk: (ini, fin) for pk, ini, fin in B.objects.filter(pk__in=ids).values_list("pk", "stock_initial_detail", "stock_final_detail")}
    tz = timezone.get_current_timezone()
    at = lambda d: timezone.make_aware(datetime.combine(d, datetime.min.time()), tz)  # noqa: E731
    bounds = {}
    for r in rows:
        d0, d1 = _file_dates(r, details)
        if d0 and d1 and d1 >= d0:
            bounds[r["site_id"]] = (at(d0), at(d1))
    tot = {sid: dict.fromkeys(RAV_KEYS, 0.0) | {"n": 0} for sid in bounds}
    if bounds:
        lo, hi = min(b[0] for b in bounds.values()), max(b[1] for b in bounds.values())
        sites = list(bounds)

        def add(sid, when, key, qty):
            t0, t1 = bounds.get(sid) or (None, None)
            if t0 and when and t0 < when <= t1 and qty is not None:
                tot[sid][key] += abs(float(qty))
                tot[sid]["n"] += 1

        # Livraisons (EYDON / Carte Total) : mouvements ENOC qualifiés et intégrés, comme le bilan
        for sid, acc, real, fam, qty in (EQ.objects.filter(site_id_resolved__in=sites, integration_status="INTEGRE")
                                         .exclude(type_family="PONCTION")
                                         .values_list("site_id_resolved", "accounting_at", "actual_operation_at", "type_family", "integrated_quantity_l")):
            add(sid, acc or real, "carte_total_l" if fam == "TOTAL_CARD" else "eydon_l", qty)
        # Ponctions (Ajout / Retrait), vols, vidanges : registre des mouvements ENOC retenus, jamais rejetés
        unbalanced = {b["transfer_id"] for b in check_transfer_balance()}
        for sid, mtype, when, qty, impact, tid in (MV.objects.filter(source_type="ENOC", site_id__in=sites, movement_type__in=list(ENOC_MOVE_KEY),
                                                                      retained_in_stock=True, movement_datetime__gt=lo, movement_datetime__lte=hi)
                                                   .exclude(status="REJECTED")
                                                   .values_list("site_id", "movement_type", "movement_datetime", "quantity_l", "impact_stock_l", "transfer_id")):
            if mtype.startswith("TRANSFERT") and tid in unbalanced:
                continue
            add(sid, when, ENOC_MOVE_KEY[mtype], impact if mtype.startswith("TRANSFERT") and impact is not None else qty)
    for r in rows:
        t = tot.get(r["site_id"])
        d0, d1 = (bounds[r["site_id"]][0].date(), bounds[r["site_id"]][1].date()) if t else (None, None)
        r["ravitaillement"] = {**({k: round(t[k], 3) for k in RAV_KEYS} if t else dict.fromkeys(RAV_KEYS)),
                               "nb_mouvements_enoc": t["n"] if t else None, "source": "ENOC" if t else None,
                               "debut": d0.isoformat() if d0 else None, "fin": d1.isoformat() if d1 else None}


def enrich(rows: list[dict], fetch: bool = True) -> str | None:
    """Ajoute à chaque ligne du tableau les blocs « inventaire » et « stock_initial_sources ». Retourne un message si Snowflake a échoué."""
    from core.models import Site

    from fuel_tracking.models import FuelSiteInventory as INV

    ids = [r["site_id"] for r in rows]
    sites = {s["site_id"].upper(): s for s in Site.objects.filter(site_id__in=ids).values(
        "site_id", "zone", "site_type", "installed_site_type", "ordered_typology", "billing_typology")}
    zones = dict(Site.ZONE_CHOICES)
    grid = {sid.upper(): g for sid, g in INV.objects.filter(site_id__in=ids).values_list("site_id", "grid_supply")}
    pairs = row_pairs(rows)
    ravitaillement(rows)
    guards, gerr = _guard_readings(pairs, fetch)
    sf, err = _snowflake_points(pairs, fetch)
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
