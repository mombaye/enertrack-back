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
Les colonnes affichées « Total QTE volée déclarée » et « Total QTE vidangée / Fuite / Écart de Stock » lisent la ligne du
fichier de clôture de la période (« Qté volée » / « Qté vidangée ») : vole_closing_l / vidange_closing_l. Case vide du fichier
ou aucune ligne de fichier : vide, jamais 0. Ce sont aussi ces valeurs du fichier qui entrent dans la variation de stock et la
synthèse (RAV_PERTES_CALCUL). Les vols / vidanges ENOC (vole_l / fuite_l) restent calculés mais sont mis de côté (non affichés,
non utilisés) : pour revenir à ENOC, remettre RAV_PERTES_CALCUL = RAV_PERTES_ENOC.
Mouvement rejeté, non intégré, ponction déséquilibrée : non compté. Aucune ligne de fichier (pas de dates) : vide, jamais 0.
"""
from __future__ import annotations

from decimal import Decimal

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
    # Jour « pas encore synchronisé » = AUCUN site n'a de faits ce jour-là (et non : le site affiché n'en a pas — un site
    # sans GE dans l'inventaire Snowflake n'a jamais de faits, ce n'est pas un retard de synchro). La synchro de ces jours
    # est lancée automatiquement en tâche de fond (voir missing_fact_days / warm_cache).
    missing_days = missing_fact_days({d for _, d in pairs})
    err = None
    if missing_days:
        err = ("Snowflake : faits du " + ", ".join(d.strftime("%d/%m/%Y") for d in missing_days[:5])
               + (" …" if len(missing_days) > 5 else "") + " en cours de synchronisation (valeurs FMS n.d. en attendant).")
        request_fact_days(missing_days)
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


def missing_fact_days(days) -> list:
    """Jours sans AUCUN fait journalier Snowflake en base (tous sites confondus)."""
    from fuel_tracking.models import FuelSiteDailyFacts as F

    days = {d for d in days if d}
    if not days:
        return []
    have = set(F.objects.filter(date__in=days).values_list("date", flat=True).distinct())
    return sorted(days - have)


def request_fact_days(days) -> None:
    """Lance en tâche de fond la synchro Snowflake (lecture seule) des jours manquants ; une seule à la fois par jeu de jours."""
    try:
        key = "fuel-facts-days:lock:" + ",".join(d.isoformat() for d in sorted(days))[:180]
        if cache.add(key, 1, 3 * 3600):   # ne pas relancer à chaque affichage : la synchro d'un jour prend quelques minutes
            from fuel_tracking.tasks import sync_fact_days

            sync_fact_days.delay([d.isoformat() for d in sorted(days)])
    except Exception as e:  # noqa: BLE001 — pas de file Celery : la synchro nocturne / horaire reste en place
        logger.warning("Synchro des jours manquants non lancée : %s", e)


def _snowflake_points(pairs, fetch: bool = True):
    """(site, jour) → {"niveau_l", "index_h"} : lecture directe des faits journaliers en base (rapide, toujours à jour de la
    dernière synchro horaire ; pas de cache nécessaire). `fetch` est sans effet (conservé pour la compatibilité)."""
    return _fetch_snowflake(pairs)


def _guard_readings(pairs, fetch: bool = True):
    """(site, jour) → relevé de gardien ENOC du jour exact : cache par paire, ENOC relu seulement pour les paires manquantes."""
    vals, err, _ = _points(PREFIX_GUARD, _fetch_guard, pairs, fetch)
    return vals, err


def row_pairs(rows, lines: dict | None = None) -> set:
    """(site, jour) aux Date de Relevé Initiale ET Finale de la ligne du fichier de chaque site (valeurs gardien / FMS lues à ces
    deux dates) ; à défaut, dates du bilan."""
    lines = lines if lines is not None else _file_line_values(rows)
    return {(r["site_id"], d) for r in rows for d in file_dates(r, lines) if d}


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


CLOSING_LOSS_KEYS = {"vole_closing_l": "vole_l", "vidange_closing_l": "vidange_l"}
# Volé / vidangé utilisés dans les calculs (variation de stock, synthèse) : fichier de clôture pour le moment.
RAV_PERTES_ENOC = ("vole_l", "fuite_l")                       # mis de côté (toujours calculé, plus utilisé)
RAV_PERTES_CALCUL = ("vole_closing_l", "vidange_closing_l")
RAV_PERTES_LIBELLE = {"vole_closing_l": "qté volée", "vidange_closing_l": "qté vidangée / fuite", "vole_l": "vols ENOC", "fuite_l": "vidanges ENOC"}


def ravitaillement_closing(rows: list[dict], lines: dict) -> None:
    """Ajoute au bloc « ravitaillement » la qté volée et la qté vidangée / fuite / écart de stock lues sur la ligne du fichier
    de clôture de la période (mêmes dates que le tableau). Valeur du fichier telle quelle ; case vide ou pas de ligne : None."""
    for r in rows:
        line = (lines.get(r["site_id"]) or {}).get("fin") or {}
        rv = r.setdefault("ravitaillement", {})
        for out_key, file_key in CLOSING_LOSS_KEYS.items():
            rv[out_key] = _f(line.get(file_key))


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
    for sid, c, at, lit, val, unit in (Obs.objects.filter(source_type="CLOSING", site_id__in=sites).exclude(validation_status="REJECTED")
                                       .values_list("site_id", "source_context", "observation_datetime", "level_liters", "level_value", "level_unit")):
        if isinstance(c, dict) and c.get("type") == "closing_periode" and c.get("periode"):
            per = (list(c["periode"]) + [None, None])[:2]
            key = (str(per[0] or "")[:10], str(per[1] or "")[:10])   # une entrée par ligne de fichier (relevés initial + final fusionnés)
            cur = lines.setdefault(sid, {}).setdefault(key, {})
            cur.update({k: v for k, v in c.items() if v is not None or k not in cur})
            niveau = lit if lit is not None else (val if unit == "L" else None)
            jour = str(at.date() if at else "")[:10]
            if niveau is not None and jour == key[0]:
                cur["_niveau_initial_l"] = float(niveau)   # relevé de la Date de Relevé Initiale de cette ligne
            if niveau is not None and jour == key[1]:
                cur["_niveau_final_l"] = float(niveau)
    out = {}
    for r in rows:
        d0, d1 = str(r["stock"].get("date_initial") or "")[:10], str(r["stock"].get("date_final") or "")[:10]
        cand = lines.get(r["site_id"], {})
        # ligne du fichier de la période : mêmes dates que le bilan, sinon même Date de Relevé Finale, sinon même Date de
        # Relevé Initiale. Ses DEUX dates font foi pour tout le tableau (stock initial, ravitaillement, temps de marche, final).
        line = (cand.get((d0, d1)) if d0 and d1 else None) \
            or next((c for (a, b), c in sorted(cand.items()) if d1 and b == d1), None) \
            or next((c for (a, b), c in sorted(cand.items()) if d0 and a == d0), None)
        out[r["site_id"]] = {"ini": line or {}, "fin": line or {}}
    return out


def closing_genset_types(lines: dict) -> dict:
    """site → (fabricant, type de GE, source affichée) lus sur la ligne du fichier de clôture de la période du tableau
    (jamais celle d'un autre mois). Ligne sans type : site absent (le type vient alors d'ENOC / Snowflake)."""
    from fuel_tracking.services.stock_file_analysis import ge_label

    out = {}
    for sid, line in lines.items():
        c = line.get("fin") or {}
        if ge_label(c.get("type_ge")):
            per = [str(x or "?")[:10] for x in (list(c.get("periode") or []) + [None, None])[:2]]
            out[sid.upper()] = (c.get("fabricant_ge"), ge_label(c["type_ge"]),
                                f"Fichier de clôture ({c.get('fichier') or 'Closing'}, {per[0]} → {per[1]})")
    return out


def file_dates(r: dict, lines: dict) -> tuple[date | None, date | None]:
    """(Date de Relevé Initiale, Date de Relevé Finale) de la ligne du fichier du site ; à défaut, dates du bilan."""
    per = (((lines.get(r["site_id"]) or {}).get("fin") or {}).get("periode") or [None, None]) + [None, None]
    return _day(per[0]) or _day(r["stock"].get("date_initial")), _day(per[1]) or _day(r["stock"].get("date_final"))


def _fms_runtimes(rows, lines) -> dict:
    """site → temps de marche GE (h) sur la plage du fichier (lendemain de la date initiale → date finale), depuis les faits
    journaliers Snowflake en base : « Alarme day_dg_on » (GENSET_REPORT.DG_RUNTIME_CALCULATED) et « Alarme day_rectifier_on »
    (règle de l'équipe data : créneaux 5 min au statut Good / Aged / Warning × 5/60 h, Down = 0 — RECTIFIER_EFFICIENCY_STATUS
    joint sur DATA_ID, tous les sites ; disponibilité = créneaux reçus ÷ 288 par jour). Jour hors 0-24 h ou absent : non compté
    (nombre de jours renseignés conservé) ; aucun jour : n.d., jamais 0. Sur un site raccordé au réseau, ces heures couvrent
    réseau + GE : affichées, mais jamais retenues comme temps de marche GE."""
    from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV
    from fuel_tracking.services.cph_engine import RUNTIME_MAX_H, SLOT_H, is_off_grid

    span = {}
    for r in rows:
        sid = r["site_id"].upper()
        li, lf = (lines.get(r["site_id"]) or {}).get("ini") or {}, (lines.get(r["site_id"]) or {}).get("fin") or {}
        d0, d1 = file_dates(r, lines)
        if d0 and d1 and d1 > d0:
            span[sid] = (d0, d1)
    if not span:
        return {}
    grid = {s.upper(): g for s, g in INV.objects.filter(site_id__in=list(span)).values_list("site_id", "grid_supply")}
    lo, hi = min(a for a, _ in span.values()), max(b for _, b in span.values())
    acc = {s: {"dg_on": 0.0, "dg_on_jours": 0, "rect": 0.0, "rect_jours": 0, "rect_slots": 0} for s in span}
    for sid, d, dg_on, slots, active in (F.objects.filter(site_id__in=list(span), date__gt=lo, date__lte=hi)
                                         .values_list("site_id", "date", "dg_on_runtime_h", "rectifier_slots", "rectifier_active_slots")):
        s = sid.upper()
        d0, d1 = span.get(s, (None, None))
        if not d0 or not (d0 < d <= d1):
            continue
        if dg_on is not None and 0 <= dg_on <= RUNTIME_MAX_H:
            acc[s]["dg_on"] += float(dg_on)
            acc[s]["dg_on_jours"] += 1
        if slots and active is not None and active * SLOT_H <= RUNTIME_MAX_H:
            acc[s]["rect"] += float(active * SLOT_H)
            acc[s]["rect_jours"] += 1
            acc[s]["rect_slots"] += int(slots)
    out = {}
    for s, a in acc.items():
        off = is_off_grid(grid.get(s))
        out[s] = {"jours": (span[s][1] - span[s][0]).days,
                  "dg_on": round(a["dg_on"], 2) if a["dg_on_jours"] else None, "dg_on_jours": a["dg_on_jours"],
                  "rect": round(a["rect"], 2) if a["rect_jours"] else None, "rect_jours": a["rect_jours"],
                  "rect_dispo_pct": round(100 * a["rect_slots"] / (288 * (span[s][1] - span[s][0]).days), 1),
                  "rect_off_grid": off,
                  "rect_motif": None if a["rect_jours"] else "aucune donnée redresseur (RECTIFIER_EFFICIENCY_STATUS) sur la période"}
    return out


def _power_medians(rows, lines, kinds: dict, kva: dict) -> dict:
    """site → puissances médianes mesurées (kW) sur la plage du fichier (lendemain de la date initiale → date finale), depuis les
    faits journaliers Snowflake en base (aucun appel Snowflake) :
      redresseur : règle équipe data (23/11/2026), RECTIFIER_EFFICIENCY_STATUS : par mesure P_DC ÷ (rendement/100) ÷ 1000,
                   mesures valides (Good/Aged/Warning, P_DC > 0, rendement 70-100 %) ; jour qualifié si ≥ 50 % de 288 pas.
                   Retenue : 1. médiane des médianes des jours qualifiés (RECTIFIER_MEDIAN_DAY) ; 2. sinon médiane de
                   toutes les mesures valides de la période (RECTIFIER_MEDIAN_PERIOD) ; 3. sinon null
                   (PUISSANCE_REDRESSEUR_INDISPONIBLE) avec le motif, jamais 0 ;
      grid       : médiane des puissances AC journalières (AC_METER.ACT_ACTIVE_POWER_AVG, déjà en kW), mesures > 0 ;
      générateur : médiane, sur les jours où le GE a tourné, de DG_PRODUCTION_KWH ÷ DG_RUNTIME_CALCULATED (GENSET_REPORT) ;
                   jour au-delà de 105 % × kVA × 0,8 écarté (kVA = fichier Base GE, sinon PRP GENSET DB) ;
      min / max (redresseur, générateur, grid) : plus petite et plus grande des valeurs qui servent à la médiane ;
      load DC telco : médiane des LOAD_W journaliers (AVERAGE_LOAD, W → kW), mesures > 0 ;
      recharge batterie : médiane du jour de l'équipe data (GOLD.BATTERY_REPORT) — voir _batt_power ;
    Aucune mesure : n.d. (jamais 0) avec la raison."""
    from statistics import median

    from fuel_tracking.models import FuelSiteDailyFacts as F
    from fuel_tracking.services import cph_power_v2 as PW
    from fuel_tracking.services.cph_engine import RUNTIME_MAX_H

    span = {r["site_id"].upper(): file_dates(r, lines) for r in rows}
    span = {k: v for k, v in span.items() if v[0] and v[1] and v[1] > v[0]}
    if not span:
        return {}
    lo, hi = min(a for a, _ in span.values()), max(b for _, b in span.values())
    facts: dict = {}
    for f in (F.objects.filter(site_id__in=list(span), date__gt=lo, date__lte=hi)
              .values("site_id", "date", "p_dc_day_kw", "p_dc_ge_tracker_kw", "p_dc_rect_active_kw", "eff_ge_tracker",
                      "eff_rect_active", "ac_active_power_avg_w", "dg_on_runtime_h", "dg_production_kwh", "load_dc_w",
                      "battery_telemetry_points", "battery_charge_slots", "battery_charge_median_kw",
                      "battery_charge_median_current_a", "battery_charge_median_voltage_v", "battery_charge_values_kw",
                      "rect_power_points",
                      "rect_power_median_kw", "rect_power_values_kw", "measured_conso_l")):
        sid = f["site_id"].upper()
        d0, d1 = span.get(sid, (None, None))
        if d0 and d0 < f["date"] <= d1:
            facts.setdefault(sid, {})[f["date"]] = f   # plusieurs DATA_ID pour un site : dernier lu (rare)
    # Niveau de cuve FMS de fin de journée, relevé initial INCLUS (conso fuel sensor par le niveau)
    levels: dict = {}
    for sid, d, lvl in (F.objects.filter(site_id__in=list(span), date__gte=lo, date__lte=hi, fms_level_l__isnull=False)
                        .values_list("site_id", "date", "fms_level_l")):
        s_ = sid.upper()
        d0, d1 = span.get(s_, (None, None))
        if d0 and d0 <= d <= d1:
            levels.setdefault(s_, {})[d] = lvl
    out = {}
    for sid, (d0, d1) in span.items():
        fx = facts.get(sid) or {}
        load = [float(f["load_dc_w"]) / 1000 for f in fx.values() if f["load_dc_w"] is not None and f["load_dc_w"] > 0]
        bp = _batt_power(fx.values(), (d1 - d0).days)
        rp = _rect_power(fx.values(), (d1 - d0).days)
        # AC_METER.ACT_ACTIVE_POWER_AVG est déjà en kW (vérifié en prod : BKL_0086 ≈ 24 kW brut) — aucune division
        grid = [float(f["ac_active_power_avg_w"]) for f in fx.values() if f["ac_active_power_avg_w"] is not None and f["ac_active_power_avg_w"] > 0]
        # Générateur : puissance réellement débitée par le GE quand il tourne, jour par jour
        # (GENSET_REPORT : DG_PRODUCTION_KWH ÷ DG_RUNTIME_CALCULATED), puis médiane sur la période.
        cap = PW.cap_kw(kva.get(sid)) if kva.get(sid) else None
        ge_days, hors_plafond, en_marche = [], 0, 0
        for f in fx.values():
            h, kwh = f["dg_on_runtime_h"], f["dg_production_kwh"]
            if h is None or kwh is None or not (0 < h <= RUNTIME_MAX_H) or kwh < 0:
                continue
            en_marche += 1
            kw = float(kwh) / float(h)
            if cap is not None and kw > cap:
                hors_plafond += 1   # jour incohérent avec la puissance nominale du GE : écarté, jamais retouché
                continue
            ge_days.append(kw)
        ge, ge_motif = (round(median(ge_days), 2) if ge_days else None), None
        if not ge_days:
            if en_marche:
                ge_motif = f"{hors_plafond} jour(s) de marche au-delà de 105 % × kVA × 0,8 ({cap:.1f} kW) : écartés"
            elif any(f["dg_on_runtime_h"] is not None for f in fx.values()):
                ge_motif = "GE à l'arrêt sur la période (Day DG On = 0 h)"
            else:
                ge_motif = "aucune donnée GENSET_REPORT sur la période"
        out[sid] = {**rp,
                    "p_grid_kw": round(median(grid), 2) if grid else None, "p_grid_jours": len(grid),
                    "p_grid_min_kw": round(min(grid), 2) if grid else None, "p_grid_max_kw": round(max(grid), 2) if grid else None,
                    "p_ge_kw": ge, "p_ge_jours": len(ge_days), "p_ge_motif": ge_motif,
                    "p_ge_min_kw": round(min(ge_days), 2) if ge_days else None, "p_ge_max_kw": round(max(ge_days), 2) if ge_days else None,
                    "p_load_dc_kw": round(median(load), 2) if load else None, "p_load_dc_jours": len(load),
                    "p_load_dc_motif": None if load else "aucune donnée AVERAGE_LOAD (load DC) sur la période",
                    **bp, **_fuel_sensor_conso(levels.get(sid) or {}, d0, d1),
                    **_fuel_sensor_conso_avant(fx.values(), (d1 - d0).days)}
    return out


def _fuel_sensor_conso_avant(facts, nb_jours: int) -> dict:
    """ANCIEN calcul de la conso fuel sensor, gardé en colonne « Avant » pour comparaison (décision du 27/11/2026) : somme des
    baisses que Snowflake a lui-même signalées jour par jour (VW_FUEL_REPORT : QUALITY_STATUS = 'OK', VALID_POINT_COUNT ≥ 2,
    DROP_DETECTED = TRUE → volume de la baisse ; ni baisse ni remplissage signalé → 0 L ; sinon jour non compté).
    Aucun jour mesurable → null (jamais 0) ; jours manquants → réserve. N'est plus utilisé dans aucun calcul."""
    vals = [float(f["measured_conso_l"]) for f in facts if f.get("measured_conso_l") is not None]
    detectes = sum(1 for v in vals if v > 0)
    if not vals:
        return {"conso_fuel_sensor_avant_l": None, "conso_fuel_sensor_avant_jours": 0, "conso_fuel_sensor_avant_baisses": 0,
                "conso_fuel_sensor_avant_motif": "aucun jour mesurable par le fuel sensor (VW_FUEL_REPORT) sur la période",
                "conso_fuel_sensor_avant_reserve": False}
    manque = max(nb_jours - len(vals), 0)
    return {"conso_fuel_sensor_avant_l": round(sum(vals), 2), "conso_fuel_sensor_avant_jours": len(vals),
            "conso_fuel_sensor_avant_baisses": detectes,
            "conso_fuel_sensor_avant_motif": f"{manque} jour(s) sur {nb_jours} sans mesure fuel sensor : somme partielle" if manque else None,
            "conso_fuel_sensor_avant_reserve": bool(manque)}


# Conso fuel sensor par le niveau de cuve (validée par le Fuel Manager le 26/11/2026) : les drapeaux DROP_DETECTED /
# REFILL_DETECTED de VW_FUEL_REPORT ne se déclenchent presque jamais (vérifié en lecture seule sur octobre 2026 : BKL_0675,
# niveau 580 → 292 L sans aucune baisse détectée). Hausse d'au moins 50 L d'un jour à l'autre = remplissage ; en dessous = bruit.
SEUIL_REMPLISSAGE_CAPTEUR_L = 50.0


def _fuel_sensor_conso(levels: dict, d0, d1) -> dict:
    """Consommation Snowflake fuel sensor sur la période, à partir du niveau de cuve de fin de journée (LAST_VALID_LEVEL,
    faits journaliers en base) : niveau au relevé initial − niveau au relevé final + remplissages vus par le capteur
    (hausse ≥ 50 L entre deux jours relevés consécutifs). Niveau absent à l'une des deux dates → null avec la raison
    (jamais 0) ; jours sans niveau entre les deux → réserve (un remplissage peut ne pas avoir été vu)."""
    nb_jours = (d1 - d0).days if d0 and d1 else 0
    base = {"conso_fuel_sensor_jours_periode": nb_jours, "conso_fuel_sensor_niveau_debut_l": None, "conso_fuel_sensor_niveau_fin_l": None,
            "conso_fuel_sensor_remplissages": 0, "conso_fuel_sensor_remplissages_l": None}
    serie = sorted((d, float(v)) for d, v in (levels or {}).items() if v is not None and d0 <= d <= d1) if d0 and d1 else []
    l0 = next((v for d, v in serie if d == d0), None)
    l1 = next((v for d, v in serie if d == d1), None)
    jours = sum(1 for d, _ in serie if d > d0)
    if l0 is None or l1 is None:
        manque = " et ".join(x for x, v in (("au relevé initial", l0), ("au relevé final", l1)) if v is None)
        return {**base, "conso_fuel_sensor_l": None, "conso_fuel_sensor_jours": jours, "conso_fuel_sensor_reserve": False,
                "conso_fuel_sensor_niveau_debut_l": l0, "conso_fuel_sensor_niveau_fin_l": l1,
                "conso_fuel_sensor_motif": f"niveau de cuve FMS absent {manque} (VW_FUEL_REPORT)"}
    sauts = [b - a for (_, a), (_, b) in zip(serie, serie[1:]) if b - a >= SEUIL_REMPLISSAGE_CAPTEUR_L]
    rempli = sum(sauts)
    conso = round(l0 - l1 + rempli, 2)
    manque = max(nb_jours - jours, 0)
    motifs = []
    if manque:
        motifs.append(f"{manque} jour(s) sur {nb_jours} sans niveau FMS : un remplissage a pu ne pas être vu")
    if conso < 0:
        motifs.append("niveau final au-dessus du niveau initial sans remplissage ≥ 50 L vu par le capteur : à vérifier")
    return {**base, "conso_fuel_sensor_l": conso, "conso_fuel_sensor_jours": jours, "conso_fuel_sensor_reserve": bool(motifs),
            "conso_fuel_sensor_motif": " ; ".join(motifs) or None, "conso_fuel_sensor_niveau_debut_l": round(l0, 2),
            "conso_fuel_sensor_niveau_fin_l": round(l1, 2), "conso_fuel_sensor_remplissages": len(sauts),
            "conso_fuel_sensor_remplissages_l": round(rempli, 2) if sauts else 0.0}


# Sélection de la puissance retenue et consommation théorique (bloc ESTIMATION)
SEL_SRC_GE, SEL_SRC_RECT = "PUISSANCE_MEDIANE_GE", "PUISSANCE_MEDIANE_REDRESSEUR"
SEL_SRC_LOAD_BATT, SEL_SRC_LOAD = "LOAD_DC_PLUS_RECHARGE_BATTERIE", "LOAD_DC_SEUL"
SEL_SRC_GRID = "PUISSANCE_GRID_MEDIANE"   # dernier recours (décision du 26/11/2026) : toutes les autres puissances n.d.
TAUX_CHARGE_MAX = 1.05            # au-delà de 105 % de la PRP : puissance incohérente avec le GE, aucune conso calculée
TAUX_CHARGE_EXTRAPOLATION = 0.5   # en dessous de 50 % : extrapolation de la parabole (points 50 / 75 / 100 %)


def theoretical_conso(e: dict) -> dict:
    """Bloc ESTIMATION, à partir des champs déjà calculés de la ligne :
    1. puissance retenue = max(Puissance médiane GE, Puissance médiane redresseur) — la médiane redresseur est déjà
       P_DC ÷ rendement mesuré (règle équipe data), elle n'est pas redivisée ; si aucune des deux : Load DC telco +
       Recharge batterie (Load DC seul, avec réserve, tant que la recharge batterie est n.d.) ; si toutes ces puissances
       manquent : Puissance Grid médiane mesurée (compteur AC : charge du site que le GE alimente quand il tourne) ;
    2. taux de charge = puissance retenue ÷ Puissance PRP (kW) du GE ;
    3. CPH (L/h) = a·x² + b·x + c (x = taux de charge en fraction, a, b, c de GENSET DB selon le type de GE) ;
       conso théorique (L) = CPH × Temps de marche GE retenu.
    Une étape impossible → null avec la raison, jamais 0."""
    out = {"p_retenue_kw": None, "p_retenue_source": None, "p_retenue_motif": None, "p_retenue_reserve": False,
           "taux_charge": None, "taux_charge_motif": None, "cph_l_h": None, "cph_a": None, "cph_b": None, "cph_c": None,
           "cph_extrapolation": False, "conso_theorique_l": None, "conso_theorique_motif": None}
    ge, rect = e.get("p_ge_kw"), e.get("p_rect_kw")
    cand = [(v, src) for v, src in ((ge, SEL_SRC_GE), (rect, SEL_SRC_RECT)) if v is not None and v > 0]
    if cand:
        out["p_retenue_kw"], out["p_retenue_source"] = max(cand, key=lambda x: x[0])
    elif e.get("p_load_dc_kw") is not None:
        batt = e.get("p_batt_kw")
        out["p_retenue_kw"] = round(e["p_load_dc_kw"] + (batt or 0), 2)
        out["p_retenue_source"] = SEL_SRC_LOAD_BATT if batt is not None else SEL_SRC_LOAD
        if batt is None:
            out["p_retenue_reserve"] = True
            out["p_retenue_motif"] = "recharge batterie n.d. : Load DC telco seul (puissance probablement sous-estimée)"
    elif e.get("p_grid_kw") is not None and e["p_grid_kw"] > 0:
        out["p_retenue_kw"], out["p_retenue_source"] = e["p_grid_kw"], SEL_SRC_GRID
        out["p_retenue_motif"] = "puissances GE, redresseur et load DC n.d. : Puissance Grid médiane mesurée retenue"
    else:
        out["p_retenue_motif"] = "aucune puissance médiane (GE, redresseur, load DC, Grid) sur la période"
        out["taux_charge_motif"] = out["conso_theorique_motif"] = "puissance retenue n.d."
        return out
    p = out["p_retenue_kw"]
    prp, curves = e.get("prp_kw") or [], e.get("courbes_cph") or []
    if len(prp) > 1 or len(curves) > 1:
        out["taux_charge_motif"] = out["conso_theorique_motif"] = "plusieurs GE sur le site : GE en marche non identifié"
        return out
    if not prp:
        out["taux_charge_motif"] = out["conso_theorique_motif"] = "Puissance PRP (kW) n.d." + (f" ({e['prp_motif']})" if e.get("prp_motif") else "")
        return out
    x = p / prp[0]
    if x > TAUX_CHARGE_MAX:
        out["taux_charge_motif"] = out["conso_theorique_motif"] = (
            f"puissance retenue {p:g} kW > 105 % de la PRP ({prp[0]:g} kW) : incohérente avec le GE")
        return out
    out["taux_charge"] = round(x, 4)
    abc = curves[0] if curves else None
    if not abc:
        out["conso_theorique_motif"] = f"courbe a, b, c du type « {e.get('type_ge')} » absente ou ambiguë dans GENSET DB"
        return out
    a, b, c = abc
    out["cph_a"], out["cph_b"], out["cph_c"] = a, b, c
    cph = a * x * x + b * x + c
    out["cph_extrapolation"] = x < TAUX_CHARGE_EXTRAPOLATION
    if cph <= 0:
        out["conso_theorique_motif"] = f"CPH ≤ 0 à {x * 100:.1f} % de charge (hors domaine de la courbe)"
        return out
    out["cph_l_h"] = round(cph, 3)
    if e.get("tm_retenu_h") is None:
        out["conso_theorique_motif"] = "Temps de marche GE retenu n.d."
        return out
    out["conso_theorique_l"] = round(cph * e["tm_retenu_h"], 2)
    if out["cph_extrapolation"]:
        out["conso_theorique_motif"] = "charge < 50 % : estimation mathématique (extrapolation de la courbe)"
    return out


RETENUE_SRC_CPH = "calcul cph et temps de marche GE"


def retained_estimation(e: dict) -> dict:
    """Estimation Conso Fuel Théorique retenue (L) et sa source, règle d'Ops (fichier Output App Fuel, vérifiée sur
    3 409 lignes) : la conso théorique CPH × Temps de marche GE retenu, et elle seule. La conso fuel sensor n'est jamais
    retenue à sa place, même quand le CPH n'est pas calculable (deux sources distinctes, jamais mélangées).
    Conso théorique n.d. → null + la même raison, source null ; 0 reste 0."""
    v = e.get("conso_theorique_l")
    if v is None:
        return {"conso_retenue_l": None, "conso_retenue_source": None,
                "conso_retenue_motif": e.get("conso_theorique_motif") or "Consommation théorique n.d."}
    return {"conso_retenue_l": v, "conso_retenue_source": RETENUE_SRC_CPH, "conso_retenue_motif": None}


# Bloc « ANALYSE VARIATION DE STOCK Vs ESTIMATION THÉORIQUE » (décision du 26/11/2026, règles retrouvées sur les 3 408 lignes
# du fichier Output App Fuel d'Ops d'octobre 2026) ; seuil « Grande différence » : |DELTA| > 100 L (Ops : 51 « Oui » ≥ 102 L,
# 350 « Non » ≤ 99 L, aucune ligne à 100 L).
SEUIL_GRANDE_DIFFERENCE_L = 100.0
# Alerte App Fuel (process de validation conso fuel, novembre 2026) : site conforme si
# |Variation de stock − Consommation théorique| ≤ max(50 L ; 5 % de la consommation théorique).
ALERTE_TOLERANCE_MIN_L, ALERTE_TOLERANCE_PCT = 50.0, 0.05
# Seuil de tolérance choisi à l'écran (liste déroulante de la colonne « Seuil de tolérance Alerte ») et appliqué à TOUS les
# sites : code « <minimum L>_<pourcentage> » → max(minimum ; pourcentage × conso théorique). Code inconnu ou absent : règle du
# process, max(50 L ; 5 %).
SEUIL_ALERTE_DEFAUT = "50_5"
SEUILS_ALERTE = {
    "50_5": "max(50 L ; 5 % de la conso théorique) — règle du process",
    "50_10": "max(50 L ; 10 % de la conso théorique)",
    "100_5": "max(100 L ; 5 % de la conso théorique)",
    "100_10": "max(100 L ; 10 % de la conso théorique)",
    "50_0": "50 L fixe",
    "100_0": "100 L fixe",
    "0_5": "5 % de la conso théorique",
    "0_10": "10 % de la conso théorique",
}


def seuil_alerte(code: str | None) -> tuple[str, float, float]:
    """Code de seuil → (code retenu, minimum en L, pourcentage en fraction)."""
    code = code if code in SEUILS_ALERTE else SEUIL_ALERTE_DEFAUT
    mini, pct = code.split("_")
    return code, float(mini), float(pct) / 100


def seuils_alerte_options(code: str | None) -> dict:
    """Liste déroulante de l'écran : seuil appliqué + choix possibles."""
    return {"code": seuil_alerte(code)[0], "defaut": SEUIL_ALERTE_DEFAUT,
            "options": [{"code": k, "libelle": v} for k, v in SEUILS_ALERTE.items()]}


def _seuil_formule(mini: float, pct: float, theo: float) -> str:
    p = f"{pct * 100:g} % × {_l0(theo)}"
    return f"max({mini:g} L ; {p})" if mini and pct else (f"{mini:g} L fixe" if mini else p)
ALERTE_CONFORME, ALERTE_ECART = "Conforme", "En écart"
# Variation de stock négative (décision du 26/11/2026) : alerte à part, prioritaire, même sans conso théorique — physiquement
# impossible (plus de carburant à la fin que début + entrées) : donnée de stock ou mouvement à corriger avant toute comparaison.
ALERTE_VAR_NEGATIVE = "Variation négative"


def stock_vs_theoretical(r: dict, seuil: str | None = None) -> dict:
    """Ligne enrichie → bloc « analyse_variation » :
      - conso_theorique_retenue_l : Estimation Conso Fuel Théorique retenue (bloc ESTIMATION) ;
      - variation_l : stock initial retenu + EYDON + CARTE TOTAL + Ajout − Retrait − Volé − Vidangé / Fuite − stock final retenu,
        seulement si les deux stocks retenus existent, que les mouvements ENOC ont été lus et que le volé / vidangé du fichier de
        clôture est renseigné (même règle que la synthèse) ; volé / vidangé : RAV_PERTES_CALCUL (fichier de clôture) ;
        négative conservée ;
      - delta_l = conso théorique retenue − variation de stock ;
      - grande_difference : « Oui » si |DELTA| > 100 L, « Non » sinon ;
      - alerte : « Conforme » si |DELTA| ≤ tolérance, « En écart » sinon (à instruire) ; tolérance = seuil choisi à l'écran
        (`seuil`, voir SEUILS_ALERTE), par défaut max(50 L ; 5 % × conso théorique).
    Une valeur absente reste null (jamais 0), avec sa raison."""
    f = lambda v: None if v is None else float(v)  # noqa: E731
    e = r.get("estimation_theorique") or {}
    rv = r.get("ravitaillement") or {}
    ini, fin = f((r.get("stock_initial_sources") or {}).get("retenue_l")), f((r.get("stock_final_sources") or {}).get("retenue_l"))
    theo = f(e.get("conso_retenue_l"))
    theo_motif = None if theo is not None else (e.get("conso_retenue_motif") or "Consommation théorique n.d.")
    if ini is None or fin is None:
        var, var_motif = None, " et ".join(m for m, v in (("stock initial retenu n.d.", ini), ("stock final retenu n.d.", fin)) if v is None)
    elif rv.get("source") != "ENOC":
        var, var_motif = None, "mouvements ENOC non lus (dates de relevé absentes du fichier)"
    elif any(rv.get(k) is None for k in RAV_PERTES_CALCUL):
        manque = " et ".join(RAV_PERTES_LIBELLE[k] for k in RAV_PERTES_CALCUL if rv.get(k) is None)
        var, var_motif = None, f"{manque} non renseignée dans le fichier de clôture"
    else:
        var = ini + sum(f(rv.get(k)) or 0 for k in ("eydon_l", "carte_total_l", "ajout_l")) \
            - sum(f(rv.get(k)) or 0 for k in ("retrait_l", *RAV_PERTES_CALCUL)) - fin
        var, var_motif = round(var, 3), None
    seuil, mini, pct = seuil_alerte(seuil)
    tol = round(max(mini, pct * abs(theo)), 3) if theo is not None else None
    tol_formule = f"{_seuil_formule(mini, pct, theo)} = {_l0(tol)}" if tol is not None else None
    alerte = alerte_motif = alerte_detail = None
    if theo is None or var is None:
        delta, grand = None, None
        motif = " ; ".join(m for m in (theo_motif and f"Conso théorique : {theo_motif}", var_motif and f"Variation de stock : {var_motif}") if m)
    else:
        delta = round(theo - var, 3)
        grand, motif = ("Oui" if abs(delta) > SEUIL_GRANDE_DIFFERENCE_L else "Non"), None
        ok = abs(delta) <= tol
        alerte = ALERTE_CONFORME if ok else ALERTE_ECART
        alerte_motif = (f"écart {abs(delta):.0f} L {'≤' if ok else '>'} tolérance {tol:.0f} L ({_seuil_formule(mini, pct, theo)})"
                        + ("" if ok else " : à instruire (vol, stock terrain incorrect ou mouvement mal tracé)"))
        # Détail en langage clair (décision du 26/11/2026) : ce qui s'est passé, la tolérance, quoi vérifier
        tol_txt = f"tolérance {_l0(tol)}" + ("" if not pct or tol == mini else f" = {pct * 100:g} % de la conso théorique")
        if ok:
            alerte_detail = (f"Écart de {_l0(abs(delta))} entre le stock consommé ({_l0(var)}) et la conso théorique du GE "
                             f"({_l0(theo)}) : dans la {tol_txt}. Rien à faire.")
        elif delta < 0:
            alerte_detail = (f"Le stock a baissé de {_l0(var)} alors que le GE aurait dû consommer {_l0(theo)} : "
                             f"{_l0(abs(delta))} de trop ({tol_txt}). À vérifier sur site : vol, stock relevé faux "
                             "ou mouvement mal saisi dans ENOC.")
        else:
            alerte_detail = (f"Le stock a baissé de {_l0(var)} alors que le GE aurait dû consommer {_l0(theo)} : "
                             f"{_l0(abs(delta))} de moins que prévu ({tol_txt}). À vérifier sur site : stock relevé faux, "
                             "mouvement mal saisi dans ENOC, ou site qui fonctionne anormalement.")
    if var is not None and var < 0:
        alerte = ALERTE_VAR_NEGATIVE
        alerte_motif = f"variation de stock négative ({_l0(var)}) : stock ou mouvement à corriger avant comparaison"
        alerte_detail = (f"Le stock a augmenté de {_l0(abs(var))} de plus que les livraisons enregistrées : c'est impossible. "
                         "Une livraison ou un ajout manque dans ENOC, ou un relevé de stock (début ou fin) est faux. "
                         "À corriger avant de comparer avec la conso théorique.")
    return {"conso_theorique_retenue_l": theo, "conso_theorique_motif": theo_motif, "variation_l": var, "variation_motif": var_motif,
            "delta_l": delta, "grande_difference": grand, "motif": motif, "seuil_l": SEUIL_GRANDE_DIFFERENCE_L,
            "tolerance_l": tol, "tolerance_formule": tol_formule, "seuil_code": seuil, "alerte": alerte, "alerte_motif": alerte_motif or (motif if alerte is None else None),
            "alerte_detail": alerte_detail or (f"Calcul impossible : {motif}." if motif else None)}


def _l0(v: float) -> str:
    """Litres arrondis à l'unité, séparateur de milliers français : 1234.6 → « 1 235 L »."""
    return f"{round(v):,} L".replace(",", " ")


BATT_SRC_DAY, BATT_SRC_PERIOD = "BATTERY_CHARGE_MEDIAN_DAY", "BATTERY_CHARGE_MEDIAN_PERIOD"
BATT_SRC_NO_TELEMETRY, BATT_SRC_NO_CHARGE = "BATTERY_CHARGE_TELEMETRY_UNAVAILABLE", "NO_BATTERY_CHARGING_MEASURED"
BATT_SRC_NONE = "BATTERY_CHARGE_POWER_UNAVAILABLE"
BATT_LOW_POSITIVE_INTERVALS = 12   # moins d'1 h de charge mesurée sur la période : réserve affichée (seuil à confirmer)


def _batt_power(facts, nb_jours: int) -> dict:
    """Puissance Médiane Recharge batterie sur la période, à partir des faits journaliers (GOLD.BATTERY_REPORT : médiane du
    jour de courant × tension ÷ 1000 sur les mesures en charge, calculée par l'équipe data) ; jour qualifié si pas de 5 min
    reçus ≥ 50 % de 288.
    1. médiane des médianes des jours qualifiés ayant de la charge (BATTERY_CHARGE_MEDIAN_DAY) ;
    2. aucun jour qualifié avec charge → null (BATTERY_CHARGE_POWER_UNAVAILABLE), règle prudente validée le 24/11/2026 ;
    aucune télémétrie → null (BATTERY_CHARGE_TELEMETRY_UNAVAILABLE) ; télémétrie sans charge → null
    (NO_BATTERY_CHARGING_MEASURED). Jamais 0, jamais ajoutée à P_DC."""
    from statistics import median

    days = [f for f in facts if f.get("battery_telemetry_points")]
    obs = sum(f["battery_telemetry_points"] for f in days)
    pos = sum(f.get("battery_charge_slots") or 0 for f in days)
    qual = lambda f: f["battery_telemetry_points"] / RECT_DAY_SLOTS >= RECT_QUALIFIED_RATIO
    q = [f for f in days if qual(f) and (f.get("battery_charge_slots") or 0) > 0 and f.get("battery_charge_median_kw") is not None]
    med = lambda xs: round(median(xs), 3) if xs else None
    day_kw = med([float(f["battery_charge_median_kw"]) for f in q])
    cur = med([float(f["battery_charge_median_current_a"]) for f in q if f.get("battery_charge_median_current_a") is not None])
    volt = med([float(f["battery_charge_median_voltage_v"]) for f in q if f.get("battery_charge_median_voltage_v") is not None])
    reserve = None
    if not obs:
        kw, src, motif = None, BATT_SRC_NO_TELEMETRY, "aucune télémétrie batterie (BATTERY_REPORT) sur la période"
    elif not pos:
        kw, src = None, BATT_SRC_NO_CHARGE
        motif = f"télémétrie présente ({obs} pas de 5 min) mais aucun courant de charge positif sur la période"
    elif day_kw is not None:
        kw, src, motif = day_kw, BATT_SRC_DAY, None
    else:
        kw, src = None, BATT_SRC_NONE
        nq = sum(1 for f in days if not qual(f) and (f.get("battery_charge_slots") or 0) > 0)
        motif = (f"aucun jour ≥ {RECT_QUALIFIED_RATIO * 100:.0f} % de télémétrie avec une puissance de charge"
                 + (f" ({nq} jour(s) en charge sous ce seuil, non retenus)" if nq else
                    " (jours en charge sans puissance fournie par BATTERY_REPORT)"))
    if kw is not None and pos < BATT_LOW_POSITIVE_INTERVALS:
        reserve = f"seulement {pos} pas de 5 min en charge sur la période : médiane peu représentative"
    return {"p_batt_kw": round(kw, 3) if kw is not None else None, "p_batt_jours": len(q),
            "p_batt_median_day_kw": day_kw, "p_batt_median_period_kw": None,
            "p_batt_current_a": cur, "p_batt_voltage_v": volt,
            "p_batt_dispo_pct": round(100 * obs / (RECT_DAY_SLOTS * nb_jours), 1) if nb_jours > 0 else None,
            "p_batt_pos": pos, "p_batt_source": src, "p_batt_motif": motif, "p_batt_reserve": reserve}


RECT_DAY_SLOTS = 288          # pas de 5 min par jour
RECT_QUALIFIED_RATIO = 0.5    # jour qualifié : mesures valides ≥ 50 % des pas attendus
RECT_SRC_DAY, RECT_SRC_PERIOD, RECT_SRC_NONE = "RECTIFIER_MEDIAN_DAY", "RECTIFIER_MEDIAN_PERIOD", "PUISSANCE_REDRESSEUR_INDISPONIBLE"


def _rect_power(facts, nb_jours: int) -> dict:
    """Puissance redresseur retenue sur la période (règle équipe data) à partir des faits journaliers."""
    from statistics import median

    days = [f for f in facts if f.get("rect_power_points")]
    qualified = [float(f["rect_power_median_kw"]) for f in days
                 if f["rect_power_median_kw"] is not None and f["rect_power_points"] / RECT_DAY_SLOTS >= RECT_QUALIFIED_RATIO]
    pts = sum(f["rect_power_points"] for f in days)
    dispo = round(100 * pts / (RECT_DAY_SLOTS * nb_jours), 1) if nb_jours > 0 else None
    day_kw = round(median(qualified), 3) if qualified else None
    # Médiane de la période : toutes les mesures valides, connues seulement quand aucun jour n'est qualifié (repli)
    values = [x for f in days if not (f["rect_power_points"] / RECT_DAY_SLOTS >= RECT_QUALIFIED_RATIO)
              for x in (f.get("rect_power_values_kw") or [])]
    period_kw = round(median(values), 3) if values and not qualified else None
    if day_kw is not None:
        kw, src, motif = day_kw, RECT_SRC_DAY, None
    elif period_kw is not None:
        kw, src = period_kw, RECT_SRC_PERIOD
        motif = (f"aucun jour ≥ {RECT_QUALIFIED_RATIO * 100:.0f} % de disponibilité : médiane des {len(values)} mesure(s) "
                 "valide(s) de la période")
    else:
        kw, src = None, RECT_SRC_NONE
        motif = ("aucune mesure redresseur valide sur la période (statut Good/Aged/Warning, P_DC > 0, rendement 70-100 %)"
                 if not days else "mesures redresseur valides sans détail disponible : resynchronisation nécessaire")
    used = qualified if day_kw is not None else (values if period_kw is not None else [])   # valeurs de la médiane retenue
    return {"p_rect_kw": round(kw, 2) if kw is not None else None, "p_rect_jours": len(qualified),
            "p_rect_min_kw": round(min(used), 2) if used else None, "p_rect_max_kw": round(max(used), 2) if used else None,
            "p_rect_median_day_kw": day_kw, "p_rect_median_period_kw": period_kw, "p_rect_dispo_pct": dispo,
            "p_rect_source": src, "p_rect_motif": motif}


def enrich(rows: list[dict], fetch: bool = True, seuil: str | None = None) -> str | None:
    """Ajoute à chaque ligne du tableau les blocs « inventaire » et « stock_initial_sources ». Retourne un message si Snowflake a échoué.
    `seuil` : seuil de tolérance de l'Alerte App Fuel choisi à l'écran (SEUILS_ALERTE), appliqué à toutes les lignes."""
    from core.models import Site

    from fuel_tracking.models import FuelSiteInventory as INV

    ids = [r["site_id"] for r in rows]
    sites = {s["site_id"].upper(): s for s in Site.objects.filter(site_id__in=ids).values(
        "site_id", "zone", "site_type", "installed_site_type", "ordered_typology", "billing_typology")}
    zones = dict(Site.ZONE_CHOICES)
    grid = {sid.upper(): g for sid, g in INV.objects.filter(site_id__in=ids).values_list("site_id", "grid_supply")}
    lines = _file_line_values(rows)
    pairs = row_pairs(rows, lines)
    ravitaillement(rows)
    ravitaillement_closing(rows, lines)   # qtés volée / vidangée du fichier de clôture (affichage, hors calcul de variation)
    runtimes = _fms_runtimes(rows, lines)
    _closing_ge = closing_genset_types(lines)   # type de GE lu sur la ligne du fichier de clôture du mois (2ᵉ source)
    _ge_now = __import__("fuel_tracking.services.ge_info", fromlist=["x"]).sites_info((r["site_id"] for r in rows), _closing_ge)
    from fuel_tracking.services.base_ge import latest_types as _latest_types

    _base_kva = {k: v[1] for k, v in _latest_types()[0].items() if v[1]}
    _kinds = {}
    from core.models import Site as _Site

    for _sid, _a, _b in _Site.objects.filter(site_id__in=[r["site_id"] for r in rows]).values_list("site_id", "installed_site_type", "site_type"):
        _k = _a or _b
        _kinds[_sid.upper()] = {"INDOOR": "Indoor", "OUTDOOR": "Outdoor"}.get(_k, _k)
    _kva = {s: float(_base_kva[s]) if s in _base_kva else (sum((_ge_now.get(s) or {}).get("prp_kva") or []) or None)
            for s in {r["site_id"].upper() for r in rows}}
    powers = _power_medians(rows, lines, _kinds, _kva)
    from fuel_tracking.services import ge_info as G

    ge = G.sites_info((r["site_id"] for r in rows), _closing_ge)   # Base GE en direct (dernier fichier importé) + sources en cache
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
        # Date de Relevé Initiale de la ligne du fichier du site (jamais une autre date) ; quantité de terrain = relevé de cette ligne
        d = file_dates(r, lines)[0]
        li0 = (lines.get(r["site_id"]) or {}).get("ini") or {}
        terrain = (_f(li0.get("_niveau_initial_l")) if li0.get("_niveau_initial_l") is not None
                   else (r["stock"].get("initial_l") if (r["stock"].get("source_initial") or "").lower() == "closing"
                         and str(d or "") == str(_day(r["stock"].get("date_initial")) or "") else None))
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
        # Bloc « ESTIMATION CONSOMMATION FUEL THÉORIQUE » : fabricant / type de GE (Snowflake, sinon ENOC)
        r["estimation_theorique"] = {**{"fabricant_ge": None, "type_ge": None, "source_ge": None, "prp_kva": None, "prp_kw": None,
                                        "prp_motif": None, "prp_source": None}, **(ge.get(r["site_id"].upper()) or {})}
        # Temps de marche GE (bloc ESTIMATION) — plage du fichier : lendemain de la date initiale → date finale
        rt = runtimes.get(r["site_id"].upper()) or {}
        li, lf = (lines.get(r["site_id"]) or {}).get("ini") or {}, (lines.get(r["site_id"]) or {}).get("fin") or {}
        # Temps de marche GE (relevé de terrain) = Relevé Visuel Compteur genset (relevé de terrain) à la date finale − à la date
        # initiale, les deux relevés affichés dans le tableau (mêmes valeurs que le fichier). Jamais repris d'une colonne de
        # résultat (ex. KLD_0203 : fichier 740,08 h alors que 1 448,52 − 744,44 = 704,08 h). Relevé manquant, compteur final
        # inférieur à l'initial ou écart > 24 h par jour : n.d., raison au survol.
        tm_terrain, tm_terrain_motif = None, None
        ci, cf = _f(li.get("compteur_terrain_initial_h")), _f(lf.get("compteur_terrain_final_h"))
        jours = rt.get("jours") or 0
        if ci is None or cf is None:
            tm_terrain_motif = "relevé de terrain initial ou final absent du fichier" if (ci is None) != (cf is None) else None
        elif cf < ci:
            tm_terrain_motif = f"compteur final ({cf:g} h) inférieur au compteur initial ({ci:g} h)"
        elif jours and cf - ci > 24 * jours:
            tm_terrain_motif = f"écart des compteurs incohérent ({cf - ci:.0f} h sur {jours} jours)"
        else:
            tm_terrain = round(cf - ci, 2)
        fi, ff = r["stock_initial_sources"].get("compteur_fms_h"), None
        r["estimation_theorique"].update(
            tm_dg_on_h=rt.get("dg_on"), tm_dg_on_jours=rt.get("dg_on_jours"),
            tm_rectifier_h=rt.get("rect"), tm_rectifier_jours=rt.get("rect_jours"), tm_rectifier_motif=rt.get("rect_motif"),
            tm_rectifier_dispo_pct=rt.get("rect_dispo_pct"), tm_rectifier_off_grid=rt.get("rect_off_grid"),
            tm_jours_periode=rt.get("jours"), tm_terrain_h=tm_terrain, tm_terrain_motif=tm_terrain_motif)
        # Puissances médianes mesurées (bloc ESTIMATION) — plage du fichier, faits journaliers Snowflake en base
        pw = powers.get(r["site_id"].upper()) or {}
        r["estimation_theorique"].update(p_rect_kw=pw.get("p_rect_kw"), p_rect_jours=pw.get("p_rect_jours"),
                                         p_rect_median_day_kw=pw.get("p_rect_median_day_kw"),
                                         p_rect_median_period_kw=pw.get("p_rect_median_period_kw"),
                                         p_rect_dispo_pct=pw.get("p_rect_dispo_pct"), p_rect_source=pw.get("p_rect_source"),
                                         p_rect_motif=pw.get("p_rect_motif"),
                                         p_ge_kw=pw.get("p_ge_kw"), p_ge_jours=pw.get("p_ge_jours"), p_ge_motif=pw.get("p_ge_motif"),
                                         p_grid_kw=pw.get("p_grid_kw"), p_grid_jours=pw.get("p_grid_jours"),
                                         # min / max : mêmes valeurs journalières que la médiane (même filtre, même période)
                                         **{k: pw.get(k) for k in ("p_rect_min_kw", "p_rect_max_kw", "p_ge_min_kw", "p_ge_max_kw",
                                                                   "p_grid_min_kw", "p_grid_max_kw")},
                                         p_load_dc_kw=pw.get("p_load_dc_kw"), p_load_dc_jours=pw.get("p_load_dc_jours"),
                                         p_load_dc_motif=pw.get("p_load_dc_motif"),
                                         **{k: pw.get(k) for k in ("p_batt_kw", "p_batt_jours", "p_batt_median_day_kw",
                                                                   "p_batt_median_period_kw", "p_batt_current_a", "p_batt_voltage_v",
                                                                   "p_batt_dispo_pct", "p_batt_pos", "p_batt_source",
                                                                   "p_batt_motif", "p_batt_reserve",
                                                                   "conso_fuel_sensor_l", "conso_fuel_sensor_jours",
                                                                   "conso_fuel_sensor_jours_periode", "conso_fuel_sensor_motif",
                                                                   "conso_fuel_sensor_reserve", "conso_fuel_sensor_niveau_debut_l",
                                                                   "conso_fuel_sensor_niveau_fin_l", "conso_fuel_sensor_remplissages",
                                                                   "conso_fuel_sensor_remplissages_l", "conso_fuel_sensor_avant_l",
                                                                   "conso_fuel_sensor_avant_jours", "conso_fuel_sensor_avant_baisses",
                                                                   "conso_fuel_sensor_avant_motif", "conso_fuel_sensor_avant_reserve")})
        # Bloc « DONNÉES DE STOCK FINAL », à la Date de Relevé Finale de la ligne du fichier (seule date retenue ; absente → n.d.)
        line = (lines.get(r["site_id"]) or {}).get("fin") or {}
        df = _day((line.get("periode") or [None, None])[1])   # Date de Relevé Finale du fichier (absente → n.d.)
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
        # Temps de marche GE (Index Compteur FMS) = compteur FMS à la date finale − compteur FMS à la date initiale
        ff = pf.get("index_h")
        # Temps de marche GE (Index Compteur FMS) = Relevé Visuel Compteur genset (Index Compteur FMS) final − initial (relevés affichés)
        r["estimation_theorique"]["tm_index_fms_h"] = round(ff - fi, 2) if fi is not None and ff is not None and ff >= fi else None
        r["estimation_theorique"]["tm_index_fms_motif"] = (None if fi is not None and ff is not None and ff >= fi else
                                                         "relevé FMS initial ou final absent" if (fi is None) != (ff is None) else
                                                         f"compteur FMS final ({ff:g} h) inférieur à l'initial ({fi:g} h)" if fi is not None and ff is not None else None)
        # Source de donnée Temps de marche GE : ordre appliqué par Ops dans le fichier Output App Fuel d'octobre (vérifié sur
        # 400 sites) : Index Compteur FMS → relevé de terrain → Alarme day_dg_on FMS → Alarme day_rectifier_on FMS.
        e = r["estimation_theorique"]
        e.update(retained_runtime(e))
        e.update(theoretical_conso(e))
        e.update(retained_estimation(e))
        r["analyse_variation"] = stock_vs_theoretical(r, seuil)
    return err


# Visibilité de chaque source de temps de marche GE sur la période d'observation (décision du 26/11/2026) : seule une
# source vue sur PLUS de 50 % de la période entre dans le choix du temps de marche retenu.
TM_VISIBILITE_MIN_PCT = 50.0


def retained_runtime(e: dict) -> dict:
    """Visibilité (%) de chaque source de temps de marche GE et temps de marche retenu.
      - Alarme day_dg_on FMS : jours renseignés (GENSET_REPORT) ÷ jours de la période ;
      - Alarme day_rectifier_on FMS : créneaux 5 min reçus ÷ (288 × jours de la période) ;
      - Index Compteur FMS et relevé de terrain : compteur cumulatif lu aux deux dates du fichier → 100 % quand les deux
        relevés existent et sont cohérents (l'écart couvre toute la période), sinon n.d.
    Ordre inchangé (Index Compteur FMS → relevé de terrain → day_dg_on → day_rectifier_on, ce dernier seulement hors
    réseau), mais une source à 50 % de visibilité ou moins est écartée (valeur toujours affichée, raison au survol)."""
    jours = e.get("tm_jours_periode") or 0
    pct = lambda n: round(100 * n / jours, 1) if jours > 0 and n is not None else None  # noqa: E731
    vis = {
        "tm_index_fms": 100.0 if e.get("tm_index_fms_h") is not None else None,
        "tm_terrain": 100.0 if e.get("tm_terrain_h") is not None else None,
        "tm_dg_on": pct(e.get("tm_dg_on_jours")) if e.get("tm_dg_on_h") is not None else None,
        "tm_rectifier": (e.get("tm_rectifier_dispo_pct") if e.get("tm_rectifier_h") is not None else None),
    }
    vis = {k: (min(float(v), 100.0) if v is not None else None) for k, v in vis.items()}
    ok = lambda k: vis[k] is not None and vis[k] > TM_VISIBILITE_MIN_PCT  # noqa: E731
    out = {f"{k}_visibilite_pct": v for k, v in vis.items()}
    ecartees = []
    for k, label in (("tm_index_fms", "Index Compteur FMS"), ("tm_terrain", "relevé de terrain"), ("tm_dg_on", "Alarme day_dg_on FMS"),
                     ("tm_rectifier", "Alarme day_rectifier_on FMS")):
        if e.get(f"{k}_h") is not None and not ok(k):
            ecartees.append(f"{label} ({vis[k]:g} %)" if vis[k] is not None else label)
            out[f"{k}_ecart_motif"] = f"visibilité {vis[k]:g} % ≤ {TM_VISIBILITE_MIN_PCT:g} % : non retenue" if vis[k] is not None else "visibilité n.d. : non retenue"
        else:
            out[f"{k}_ecart_motif"] = None
    g = lambda k: e.get(f"{k}_h") if ok(k) else None  # noqa: E731
    out["tm_retenu_h"], out["tm_source"] = _first_of((g("tm_index_fms"), "FMS"), (g("tm_terrain"), "relevé de terrain"),
                                                     (g("tm_dg_on"), "Alarme day_dg_on FMS"),
                                                     # redresseur retenu seulement hors réseau (sinon heures réseau + GE)
                                                     (g("tm_rectifier") if e.get("tm_rectifier_off_grid") is True else None,
                                                      "Alarme day_rectifier_on FMS"))
    out["tm_sources_ecartees"] = ecartees or None
    return out


def _first_of(*candidates):
    """Première valeur présente (jamais 0 par défaut), avec sa source ; (None, None) si aucune."""
    return next(((v, s) for v, s in candidates if v is not None), (None, None))


# ── Ligne de totaux du tableau (comme la ligne SUBTOTAL du fichier Output App Fuel / Closing) ──
TOTAL_FIELDS = {
    "stock_initial_sources": ("terrain_l", "gardiennage_l", "fms_l", "retenue_l", "compteur_terrain_h", "compteur_gardiennage_h",
                              "compteur_fms_h", "compteur_retenu_h"),
    "ravitaillement": ("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l", "vole_closing_l", "vidange_closing_l"),
    "stock_final_sources": ("terrain_l", "gardiennage_l", "fms_l", "retenue_l", "compteur_terrain_h", "compteur_gardiennage_h",
                            "compteur_fms_h", "compteur_retenu_h"),
    "estimation_theorique": ("prp_kva", "prp_kw", "tm_dg_on_h", "tm_rectifier_h", "tm_terrain_h", "tm_index_fms_h", "tm_retenu_h",
                             "p_rect_kw", "p_ge_kw", "p_grid_kw", "p_load_dc_kw", "p_batt_kw", "p_retenue_kw",
                             "conso_theorique_l", "conso_fuel_sensor_l", "conso_fuel_sensor_avant_l", "conso_retenue_l"),
    "analyse_variation": ("conso_theorique_retenue_l", "variation_l", "delta_l"),
}
# Colonnes texte : « total » = nombre de sites renseignés (case vide / « n.d. » non comptée)
TOTAL_COUNTS = {"estimation_theorique": ("fabricant_ge", "type_ge", "source_ge", "tm_source", "p_retenue_source")}
# Colonnes à deux valeurs : « total » = nombre de sites avec la valeur signalée (1ʳᵉ), `sites` = nombre de sites renseignés
TOTAL_OUI = {"analyse_variation": {"grande_difference": ("Oui", "Non"),
                                   "alerte": (ALERTE_ECART, ALERTE_CONFORME, ALERTE_VAR_NEGATIVE)}}


def enriched_copy(rows: list[dict], seuil: str | None = None) -> list[dict]:
    """Copie des lignes enrichie comme à l'affichage (cache uniquement, aucun appel Snowflake / ENOC) ; lignes d'origine intactes."""
    import copy

    rows = copy.deepcopy(rows)
    enrich(rows, fetch=False, seuil=seuil)
    return rows


def totals(rows: list[dict], enriched: bool = False) -> dict:
    """Totaux de chaque colonne chiffrée sur TOUS les sites du filtre (et non la seule page) : {bloc: {champ: {total, sites}}}.
    Valeurs lues comme à l'affichage, sans aucun appel à Snowflake / ENOC (cache uniquement). Case « n.d. » : non comptée
    (jamais 0) ; `sites` = nombre de sites ayant une valeur. Plusieurs GE sur un site : leurs puissances sont additionnées."""
    if not enriched:
        rows = enriched_copy(rows)
    out: dict = {}
    for bloc, champs in TOTAL_FIELDS.items():
        for champ in champs:
            vals = []
            for r in rows:
                v = (r.get(bloc) or {}).get(champ)
                if isinstance(v, list):
                    v = sum(x for x in v if x is not None) if any(x is not None for x in v) else None
                if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):   # médianes en Decimal
                    vals.append(float(v))
            out.setdefault(bloc, {})[champ] = {"total": round(sum(vals), 2) if vals else None, "sites": len(vals)}
    for bloc, champs in TOTAL_COUNTS.items():
        for champ in champs:
            n = sum(1 for r in rows if str((r.get(bloc) or {}).get(champ) or "").strip())
            out.setdefault(bloc, {})[champ] = {"total": n, "sites": n, "comptage": True}
    for bloc, champs in TOTAL_OUI.items():
        for champ, (flag, *others) in champs.items():
            vals = [(r.get(bloc) or {}).get(champ) for r in rows]
            out.setdefault(bloc, {})[champ] = {"total": sum(1 for v in vals if v == flag), "sites": sum(1 for v in vals if v in (flag, *others)),
                                               "oui": True, "valeur": flag}
            if len(others) > 1:   # valeurs signalées en plus (ex. « Variation négative »)
                out[bloc][champ]["autres"] = {o: sum(1 for v in vals if v == o) for o in others[1:]}
    return out
