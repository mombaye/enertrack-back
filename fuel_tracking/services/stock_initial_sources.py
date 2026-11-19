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
  Qtité (Fuel Sensor FMS)                   VW_FUEL_REPORT.LAST_VALID_LEVEL du même jour, copié chaque heure dans les faits
                                            journaliers (FuelSiteDailyFacts.fms_level_l) — jamais lu en direct pendant l'affichage ;
  Qtité retenue                             FMS si disponible, sinon relevé de terrain ;
  Compteur genset (Société de gardiennage)  compteur horaire porté par ce même relevé de gardien ENOC, s'il existe ;
  Compteur genset (Index Compteur FMS)      GENSET_REPORT.TOTAL_DG_RUNNING_HOUR du jour, copié dans les faits journaliers
                                            (FuelSiteDailyFacts.dg_total_running_h) ;
  Compteur genset (relevé de terrain)       fichier de clôture (Closing « Relevé Visuel Compteur genset à la date de relevé »,
                                            Output App Fuel « … (relevé de terrain) »), ligne du site, date initiale ;
  Compteur retenu                           Index Compteur FMS, sinon Société de gardiennage, sinon relevé de terrain.

Bloc « DONNÉES DE STOCK FINAL » : mêmes sources à la « Date de Relevé Finale » de la ligne du fichier. Quantité de terrain et
compteur de terrain : fichier de clôture (sept., oct.) ; gardiennage : ENOC du jour exact ; FMS : faits journaliers Snowflake.
Quantité retenue ET compteur retenu : FMS, sinon Société de gardiennage, sinon relevé de terrain (source affichée).
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


def _snapshot_levels(pairs) -> dict:
    """(site, jour) → niveau FMS archivé (FuelStockSnapshot, issu de VW_FUEL_REPORT) dont la date est exactement ce jour."""
    from fuel_tracking.models import FuelStockSnapshot as SNAP

    out = {}
    for sid, d, lvl in (SNAP.objects.filter(site_id__in={s for s, _ in pairs}, stock_snowflake_date__in={d for _, d in pairs},
                                             stock_snowflake_l__isnull=False)
                        .values_list("site_id", "stock_snowflake_date", "stock_snowflake_l")):
        if (sid, d) in pairs:
            out.setdefault((sid, d), set()).add(float(lvl))
    return {k: _f(next(iter(v))) for k, v in out.items() if len(v) == 1}   # valeurs contradictoires : aucune


def _fetch_snowflake(pairs) -> tuple[dict, str | None]:
    """(site, jour) → {"niveau_l", "index_h"} lus dans NOTRE base : faits journaliers copiés depuis Snowflake par la synchro
    horaire (VW_FUEL_REPORT, GENSET_REPORT). Aucun appel Snowflake ici : réponse en quelques millisecondes.

    Faits du jour présents mais valeur NULL → le capteur n'a pas de mesure ce jour-là (n.d., jamais 0). Plusieurs DATA_ID
    pour un même site avec des valeurs différentes → aucune valeur. Niveau absent des faits : niveau archivé par la synchro de
    stock s'il date exactement de ce jour. Jour pas encore synchronisé du tout : message.
    """
    from fuel_tracking.models import FuelSiteDailyFacts as F

    if not pairs:
        return {}, None
    found: dict = {}
    synced_days = set()
    for sid, d, lvl, hours in (F.objects.filter(site_id__in={s for s, _ in pairs}, date__in={d for _, d in pairs})
                               .values_list("site_id", "date", "fms_level_l", "dg_total_running_h")):
        synced_days.add(d)
        if (sid, d) in pairs:
            cur = found.setdefault((sid, d), {"niveau_l": set(), "index_h": set()})
            if lvl is not None:
                cur["niveau_l"].add(_f(lvl))
            if hours is not None:
                cur["index_h"].add(round(float(hours), 2))
    out = {}
    for k, v in found.items():
        vals = {name: next(iter(x)) for name, x in v.items() if len(x) == 1}   # contradictoire → aucune valeur
        if vals:
            out[k] = vals
    for k, lvl in _snapshot_levels(pairs).items():
        if "niveau_l" not in out.get(k, {}):
            out.setdefault(k, {})["niveau_l"] = lvl
    missing_days = sorted({d for _, d in pairs} - synced_days)
    err = None
    if missing_days:
        err = ("Snowflake : faits du " + ", ".join(d.strftime("%d/%m/%Y") for d in missing_days[:5])
               + (" …" if len(missing_days) > 5 else "") + " pas encore synchronisés (valeurs FMS n.d.).")
    return out, err


# ── Cache par (site, jour) ────────────────────────────────────────────────────────────────────────────────────────────────
# Chaque valeur lue (Snowflake FMS, ENOC gardien) est gardée par site et par jour de relevé : changer de page, passer à 200
# par page ou à « Tous les sites » réutilise ce qui a déjà été lu. Les dates de relevé sont passées : valeurs stables (24 h).
# Échec de lecture : gardé 10 min seulement, avec son message, pour réessayer ensuite sans bloquer chaque affichage.
PAIR_TTL_OK, PAIR_TTL_ERR, FETCH_CHUNK = 24 * 3600, 600, 2000
PREFIX_GUARD = "fuel-si:guard:v2"   # relevés de gardien ENOC (MongoDB) : seule source encore lue hors de notre base


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
    """(site, jour) → {"niveau_l", "index_h"} : lecture directe des faits journaliers en base (rapide, toujours à jour de la
    dernière synchro horaire ; pas de cache nécessaire). `fetch` est sans effet (conservé pour la compatibilité)."""
    return _fetch_snowflake(pairs)


def _guard_readings(pairs, fetch: bool = True):
    """(site, jour) → relevé de gardien ENOC du jour exact : cache par paire, ENOC relu seulement pour les paires manquantes."""
    vals, err, _ = _points(PREFIX_GUARD, _fetch_guard, pairs, fetch)
    return vals, err


def row_pairs(rows) -> set:
    """(site, jour) des relevés initial ET final de chaque ligne (valeurs gardien / FMS lues à ces deux dates)."""
    return {(r["site_id"], _day(r["stock"].get(k))) for r in rows for k in ("date_initial", "date_final") if r["stock"].get(k)}


def missing_pairs(pairs) -> set:
    """Paires (site, jour) dont le relevé de gardien ENOC n'est pas encore en cache (Snowflake : lu en base, jamais en attente)."""
    return _cached(PREFIX_GUARD, pairs)[1]


def warm_pairs(pairs) -> int:
    """Lit (et met en cache) les relevés de gardien ENOC des paires manquantes. Retourne le nombre de paires lues."""
    miss = missing_pairs(pairs)
    if miss:
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


def _file_final_date(r: dict, details: dict) -> str | None:
    """Date de Relevé Finale lue dans la ligne du fichier de clôture uniquement (jamais une date reprise d'ailleurs)."""
    ctx = _file_line_context(*details.get(r["stock"].get("bilan_id"), (None, None)))
    d = _day(((ctx or {}).get("periode") or [None, None])[1])
    return d.isoformat() if d else None


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
                               "debut": d0.isoformat() if d0 else None, "fin": d1.isoformat() if d1 else None,
                               # « Date de Relevé Finale » telle qu'écrite dans la ligne du fichier de clôture (contrôle) ; vide si absente
                               "date_finale_fichier": _file_final_date(r, details)}


def _file_line_values(rows) -> dict:
    """site → {"ini": contexte, "fin": contexte} : ligne du fichier de clôture (valeurs de terrain) correspondant aux dates du
    bilan, cherchée parmi TOUS les relevés de clôture du site (un relevé commun à deux fichiers — ex. le 30/09, final de
    septembre et initial d'octobre — n'est enregistré qu'une fois, rattaché à un seul fichier).

    « ini » : ligne dont la Date de Relevé Initiale est celle du bilan (de préférence avec la même date finale) ;
    « fin » : ligne dont la Date de Relevé Finale est celle du bilan (de préférence avec la même date initiale).
    Bilan sans date initiale : ligne du stock final ; sans date finale : ligne du stock initial. Aucune ligne aux bonnes dates :
    vide (jamais une autre période).
    """
    from fuel_tracking.models import FuelStockObservation as Obs

    sites = {r["site_id"] for r in rows}
    lines: dict = {}
    for sid, c in (Obs.objects.filter(source_type="CLOSING", site_id__in=sites).exclude(validation_status="REJECTED")
                   .values_list("site_id", "source_context")):
        if isinstance(c, dict) and c.get("type") == "closing_periode" and c.get("periode"):
            per = (list(c["periode"]) + [None, None])[:2]
            key = (str(per[0] or "")[:10], str(per[1] or "")[:10])   # une entrée par ligne de fichier (relevés initial + final fusionnés)
            cur = lines.setdefault(sid, {}).setdefault(key, {})
            cur.update({k: v for k, v in c.items() if v is not None or k not in cur})
    out = {}
    for r in rows:
        d0, d1 = str(r["stock"].get("date_initial") or "")[:10], str(r["stock"].get("date_final") or "")[:10]
        cand = lines.get(r["site_id"], {})
        both = cand.get((d0, d1)) if d0 and d1 else None
        fin = both or next((c for (a, b), c in sorted(cand.items()) if d1 and b == d1), None)
        ini = both or (next((c for (a, b), c in sorted(cand.items()) if a == d0), None) if d0 else fin)
        if fin is None and not d1:
            fin = ini   # bilan sans date finale : valeurs finales de la même ligne de fichier (dates du fichier)
        out[r["site_id"]] = {"ini": ini or {}, "fin": fin or {}}
    return out


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
    lines = _file_line_values(rows)
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
        line = (lines.get(r["site_id"]) or {}).get("ini") or {}
        cpt_terrain = _f(line.get("compteur_terrain_initial_h"))
        # ordre : Index Compteur FMS → Société de gardiennage → relevé de terrain du fichier de clôture
        cpt, csrc = _first_of((idx, SRC_INDEX_FMS), (g.get("compteur"), SRC_GUARD), (cpt_terrain, SRC_TERRAIN))
        r["stock_initial_sources"] = {
            "date": d.isoformat() if d else None, "terrain_l": terrain, "gardiennage_l": g.get("litres"), "fms_l": fms,
            "retenue_l": retenue, "source": src, "compteur_terrain_h": cpt_terrain, "compteur_gardiennage_h": g.get("compteur"), "compteur_fms_h": idx,
            "compteur_retenu_h": cpt, "source_compteur": csrc,
        }
        # Bloc « DONNÉES DE STOCK FINAL », à la Date de Relevé Finale de la ligne du fichier (seule date retenue ; absente → n.d.)
        line = (lines.get(r["site_id"]) or {}).get("fin") or {}
        df = _day((line.get("periode") or [None, None])[1])
        gf = guards.get((r["site_id"], df), {}) if df else {}
        pf = sf.get((r["site_id"], df), {}) if df else {}
        # quantité de terrain : colonne du fichier (Output App Fuel « relevé de terrain », Closing « Qtité laissée à la date du relevé »)
        terrain_f = _f(line.get("stock_terrain_final_l") if line.get("stock_terrain_final_l") is not None
                       else (line.get("stock_final_l") if "stock_initial_l" in line else None))   # ancien contexte Closing
        cpt_terrain_f = _f(line.get("compteur_terrain_final_h"))
        # ordre (quantité ET compteur) : Fuel Sensor / Index FMS → Société de gardiennage → relevé de terrain du fichier
        ret_f, src_f = _first_of((pf.get("niveau_l"), SRC_FMS), (gf.get("litres"), SRC_GUARD), (terrain_f, SRC_TERRAIN))
        cpt_f, csrc_f = _first_of((pf.get("index_h"), SRC_INDEX_FMS), (gf.get("compteur"), SRC_GUARD), (cpt_terrain_f, SRC_TERRAIN))
        r["stock_final_sources"] = {
            "date": df.isoformat() if df else None, "terrain_l": terrain_f, "gardiennage_l": gf.get("litres"), "fms_l": pf.get("niveau_l"),
            "retenue_l": ret_f, "source": src_f, "compteur_terrain_h": cpt_terrain_f, "compteur_gardiennage_h": gf.get("compteur"),
            "compteur_fms_h": pf.get("index_h"), "compteur_retenu_h": cpt_f, "source_compteur": csrc_f,
        }
    return err


def _first_of(*candidates):
    """Première valeur présente (jamais 0 par défaut), avec sa source ; (None, None) si aucune."""
    return next(((v, s) for v, s in candidates if v is not None), (None, None))
