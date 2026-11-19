# fuel_tracking/services/output_app_fuel.py
"""
Fichier « Output App Fuel » (feuille « Output ») — clôture mensuelle consolidée par l'équipe Ops après la campagne de
relevés terrain. Importé par le même circuit que le Closing (rubrique Configuration ou incoming/closing/) : il sert de
fichier de clôture pour le mois qu'il couvre (octobre 2026 pour le premier fichier).

Colonnes lues (ligne d'en-tête « ID du site », repérées par leur titre, jamais par leur lettre) :
  - stock INITIAL : « Date de Relevé Initiale » (N) + « Qtité laissée à la date initiale (relevé de terrain) » (O) ;
  - stock FINAL   : « Date de Relevé Finale » (AM) + « Qtité laissée retenue » (AQ) + « Source de donnée de stock » (AR),
                    statut de fiabilisation de la campagne terrain :
                      « Relevés de Terrain Technicien »            → relevé mesuré (technicien) ;
                      « Relevés de Terrain Société de Gardiennage » → relevé mesuré (gardien) ;
                      « Estimation Energy Desk »                    → ESTIMATION (jamais un relevé mesuré : bilan « avec réserve ») ;
                      « Site sans GE et sans cuve »                 → ignoré (pas un 0 L mesuré) ;
  - pour contrôle seulement (contexte du relevé, jamais compté dans le bilan) : totaux de livraisons, ajouts, retraits,
    ponctions, vols, vidanges, variation de stock considérée par Ops, conso théorique CPH retenue et sa source.

Les stocks sont enregistrés comme relevés de clôture (source CLOSING, total du site) : les règles du bilan s'appliquent
telles quelles (stock initial / final du Closing avant FUEL_APP_STOCK_START). Idempotent : même site, date, valeur = doublon.
"""
from __future__ import annotations

import re

SHEET = "Output"
SOURCE_FINAL_SKIP = "site sans ge et sans cuve"
ESTIMATE_SOURCE = "estimation energy desk"
ESTIMATION_MODE = "ESTIMATION_ENERGY_DESK_RESERVE"
FILE_LABEL = "Output App Fuel"


def _norm(v) -> str:
    return " ".join(str(v or "").replace("\n", " ").split()).strip().lower()


def _col(header: list[str], prefix: str, start: int = 0) -> int | None:
    for i in range(start, len(header)):
        if header[i].startswith(prefix):
            return i
    return None


def find_header(rows) -> tuple[int, dict] | tuple[None, None]:
    """Ligne d'en-tête du fichier Output App Fuel et index des colonnes utiles ; (None, None) si ce n'est pas ce format."""
    for n, row in enumerate(rows):
        if n > 60:
            break
        h = [_norm(v) for v in (row or [])]
        if "id du site" not in h or _col(h, "date de relevé initiale") is None or _col(h, "date de relevé finale") is None:
            continue
        i_ini, i_fin = _col(h, "date de relevé initiale"), _col(h, "date de relevé finale")
        idx = {
            "site": h.index("id du site"), "name": _col(h, "nom du site"),
            "date_initiale": i_ini, "stock_initial": _col(h, "qtité laissée à la date initiale (relevé de terrain)", i_ini),
            "compteur_initial": _col(h, "relevé visuel compteur genset (relevé de terrain)", i_ini),
            "date_finale": i_fin, "stock_final": _col(h, "qtité laissée retenue", i_fin), "source_finale": _col(h, "source de donnée de stock", i_fin),
            "eydon": _col(h, "total livraison fuel eydon"), "carte_total": _col(h, "total livraison fuel carte total"),
            "ajout": _col(h, "total ajout"), "retrait": _col(h, "total retrait"), "ponction": _col(h, "total ponction"),
            "vol": _col(h, "total qte volée"), "vidange": _col(h, "total qte vidangée"),
            "variation": _col(h, "variation de stock fuel considér"), "cph_l": _col(h, "estimation conso fuel théorique retenue"),
            "cph_source": _col(h, "source de donnée estimation conso fuel théorique"),
        }
        if None in (idx["stock_initial"], idx["stock_final"], idx["source_finale"]):
            continue
        return n, idx
    return None, None


def parse_rows(rows) -> list[dict]:
    from fuel_tracking.services import stock_import_files as F

    n, idx = find_header(rows)
    if n is None:
        raise ValueError(f"Feuille « {SHEET} » : en-tête « ID du site / Date de Relevé Initiale / Date de Relevé Finale » introuvable.")
    out = []
    for i, row in enumerate(rows[n + 1:], start=n + 2):
        row = list(row or [])
        get = lambda k: row[idx[k]] if idx.get(k) is not None and idx[k] < len(row) else None  # noqa: E731
        sid = get("site")
        if not isinstance(sid, str) or not re.match(r"^[A-Za-z0-9]+_[A-Za-z0-9]+", sid.strip()):
            continue
        num = lambda k: F.parse_number(get(k))  # noqa: E731
        out.append({
            "site": sid.strip(), "ligne": i, "site_name": get("name"),
            "date_initiale": F.parse_date(get("date_initiale")), "stock_initial_l": num("stock_initial"),
            "date_finale": F.parse_date(get("date_finale")), "stock_final_l": num("stock_final"),
            "source_finale": " ".join(str(get("source_finale") or "").split()) or None,
            "livre_l": (num("eydon") or 0) + (num("carte_total") or 0) if (num("eydon") is not None or num("carte_total") is not None) else None,
            "livraison_eydon_l": num("eydon"), "livraison_carte_total_l": num("carte_total"), "ajout_l": num("ajout"), "retrait_l": num("retrait"),
            "ponction_l": num("ponction"), "vole_l": num("vol"), "vidange_l": num("vidange"), "variation_ops_l": num("variation"),
            "conso_theorique_ops_l": num("cph_l"), "source_conso_theorique_ops": get("cph_source"),
            "compteur_terrain_initial_h": num("compteur_initial") if idx.get("compteur_initial") is not None and idx["compteur_initial"] < idx["date_finale"] else None,
        })
    return out


def sites_without_genset(month: str) -> set[str]:
    """Sites déclarés « Site sans GE et sans cuve » par le fichier Output App Fuel du mois `month` (AAAA-MM) : hors périmètre
    carburant ce mois-là (jamais comptés « sans relevé » ni « non calculable »)."""
    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.closing_upload import CANCEL_KEY, MONTH_KEY, final_stock_month

    out = set()
    for b in FuelImportBatch.objects.filter(source_type="CLOSING", summary__stocks_closing__format=FILE_LABEL):
        s = b.summary or {}
        if CANCEL_KEY in s:
            continue
        if (s.get(MONTH_KEY) or final_stock_month(b)[0]) == month:
            out |= {x.upper() for x in (s.get("stocks_closing") or {}).get("sites_sans_ge") or []}
    return out


def backfill_sites_without_genset() -> int:
    """Lots Output App Fuel importés avant le 17/11/2026 : relit la copie d'archive pour enregistrer la liste des sites sans GE."""
    from pathlib import Path

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.stock_import_files import iter_sheets
    from fuel_tracking.services.stock_registry import SiteResolver

    done = 0
    for b in FuelImportBatch.objects.filter(source_type="CLOSING", summary__stocks_closing__format=FILE_LABEL):
        st = (b.summary or {}).get("stocks_closing") or {}
        if "sites_sans_ge" in st or not b.archive_path or not Path(b.archive_path).is_file():
            continue
        resolver = SiteResolver()
        recs = []
        for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={SHEET}):
            recs = parse_rows(list(rows))
        st["sites_sans_ge"] = sorted({(resolver.resolve(r["site"]) or r["site"]).upper() for r in recs
                                      if _norm(r["source_finale"]) == SOURCE_FINAL_SKIP})
        b.summary = {**(b.summary or {}), "stocks_closing": st}
        b.save(update_fields=["summary"])
        done += 1
    return done


def process_output_file(batch, rows, file_name: str, resolver, tanks_of) -> dict:
    """Crée les relevés de clôture (stocks initial et final) à partir des lignes du fichier Output App Fuel."""
    from fuel_tracking.models import FuelStockObservation as Obs
    from fuel_tracking.services import stock_registry as R
    from fuel_tracking.services.stock_file_analysis import plausible
    from fuel_tracking.services.stock_import_files import SITE_TOTAL, as_utc_datetime, fingerprint

    data = parse_rows(rows)
    stats = {"format": FILE_LABEL, "lignes_site": len(data), "stocks_importes": 0, "doublons": 0, "sites_inconnus": 0,
             "sites_sans_ge_ignores": 0, "dates_invraisemblables": 0, "stock_final_estime_energy_desk": 0,
             "sources_stock_final": {}, "stocks_absents": 0, "capacite_depassee_a_valider": 0, "sites_sans_ge": []}
    site_ids = {resolver.resolve(r["site"]) for r in data} - {None}
    tanks = tanks_of(site_ids)
    existing = dict(Obs.objects.filter(source_type="CLOSING", site_id__in=site_ids).values_list("source_event_id", "pk"))
    for r in data:
        src = r["source_finale"] or ""
        if _norm(src) == SOURCE_FINAL_SKIP:
            stats["sites_sans_ge_ignores"] += 1
            stats["sites_sans_ge"].append((resolver.resolve(r["site"]) or r["site"]).upper())
            continue
        site_id = resolver.resolve(r["site"])
        if site_id is None:
            stats["sites_inconnus"] += 1
            continue
        stats["sources_stock_final"][src or "(vide)"] = stats["sources_stock_final"].get(src or "(vide)", 0) + 1
        periode = [r["date_initiale"].isoformat() if r["date_initiale"] else None, r["date_finale"].isoformat() if r["date_finale"] else None]
        context = {"type": "closing_periode", "fichier": FILE_LABEL, "periode": periode, "source_stock_final": src or None,
                   "livre_l": r["livre_l"], "livraison_eydon_l": r["livraison_eydon_l"], "livraison_carte_total_l": r["livraison_carte_total_l"],
                   # ajouts − retraits. « Total Ponction » (AG) n'est PAS ajouté : c'est le solde des ponctions DÉJÀ comptées
                   # dans « Total Ajout » (AE) / « Total Retrait » (AF) — l'ajouter comptait chaque ponction deux fois
                   # (ex. KDG_0884 : AF 4 360, AG −4 360). Formule Ops vérifiée : variation = O + AK − AJ − AQ, avec
                   # AK = AC + AD + AE (introduit) et AJ = AF + AH + AI (retiré) ; « ponction_l » reste en contexte (information).
                   "rajout_retrait_l": (None if r["ajout_l"] is None and r["retrait_l"] is None
                                        else (r["ajout_l"] or 0) - (r["retrait_l"] or 0)),
                   "ponction_l": r["ponction_l"], "vole_l": r["vole_l"], "vidange_l": r["vidange_l"],
                   "ponction_corrigee": True,  # rajout_retrait_l n'inclut pas la ponction (voir fuel_post_deploy, correctif du 17/11)
                   "conso_closing_l": r["variation_ops_l"], "conso_theorique_ops_l": r["conso_theorique_ops_l"],
                   "source_conso_theorique_ops": r["source_conso_theorique_ops"],
                   "compteur_terrain_initial_h": r.get("compteur_terrain_initial_h")}
        for kind, value, d in (("initial", r["stock_initial_l"], r["date_initiale"]), ("final", r["stock_final_l"], r["date_finale"])):
            if value is None or d is None:
                stats["stocks_absents"] += 1
                continue
            if not plausible(d):
                stats["dates_invraisemblables"] += 1
                continue
            estimated = kind == "final" and _norm(src) == ESTIMATE_SOURCE
            event_id = "OAF:" + fingerprint("OUTPUT_APP_FUEL", site_id, d, kind, value)
            if event_id in existing:
                Obs.objects.filter(pk=existing[event_id]).update(source_context=context)
                stats["doublons"] += 1
                continue
            existing[event_id] = None
            origin = "relevé de terrain" if kind == "initial" else (src or "source non précisée")
            inp = R.ObservationInput(
                source_type="CLOSING", source_event_id=event_id, site_id=site_id, observation_datetime=as_utc_datetime(d),
                level_raw=f"{value:g} L ({FILE_LABEL}, stock {kind})", level_value=value, level_unit="L", tank_id=None,
                source_user=origin[:255], source_reference=f"{file_name} · {SHEET} · ligne {r['ligne']}",
                comment=f"Stock {kind} du fichier {FILE_LABEL} — {origin}.", review_flags=[])
            obs, _msgs = R.build_observation(inp, [], None)  # litres natifs : pas de conversion de cuve
            obs.tank_id = SITE_TOTAL
            obs.source_context = {**context, "nature": "estimation" if estimated else "releve", "origine": origin}
            if estimated:
                # Estimation Energy Desk : jamais présentée comme un relevé mesuré (bilan « avec réserve », jamais certifiable seul).
                obs.estimated_liters, obs.estimation_mode, obs.level_liters = obs.level_value, ESTIMATION_MODE, None
                obs.estimation_detail = {"source": "Estimation Energy Desk", "fichier": FILE_LABEL}
                obs.confidence = "FAIBLE"
                obs.validation_status = Obs.ValidationStatus.PENDING
                obs.validation_reason = "Stock final estimé par l'Energy Desk (pas un relevé de cuve)."
                stats["stock_final_estime_energy_desk"] += 1
            else:
                obs.validation_status = Obs.ValidationStatus.VALIDATED
                obs.issue_codes = []
                obs.validation_reason = f"Stock fiabilisé par la campagne terrain ({origin}), fichier {FILE_LABEL}."
            caps = [t.check_capacity_l() for t in tanks.get(site_id, []) if t.validated]
            total_cap = sum(c for c in caps if c) if caps and all(caps) else None
            if total_cap and value > total_cap * 1.05:
                obs.validation_status = Obs.ValidationStatus.PENDING
                obs.issue_codes = list(dict.fromkeys((obs.issue_codes or []) + ["OVER_CAPACITY"]))
                obs.validation_reason = f"{value:.0f} L > capacité totale des cuves validées ({total_cap:.0f} L) : à vérifier."
                stats["capacite_depassee_a_valider"] += 1
            obs.batch = batch
            obs.save()
            stats["stocks_importes"] += 1
    return stats


def backfill_terrain_counters() -> int:
    """Fichiers de clôture importés avant l'ajout du compteur : relit la copie d'archive (Closing ou Output App Fuel) et complète
    le contexte des relevés de clôture avec « Relevé Visuel Compteur genset (relevé de terrain) » à la date initiale.
    Idempotent (lot marqué). Lecture seule de l'archive ; aucune valeur inventée (case vide du fichier → rien)."""
    from pathlib import Path

    from fuel_tracking.models import FuelImportBatch, FuelStockObservation as Obs
    from fuel_tracking.services.stock_file_analysis import CLOSING_SHEET, closing_stock_rows
    from fuel_tracking.services.stock_import_files import iter_sheets
    from fuel_tracking.services.stock_registry import SiteResolver

    done = 0
    for b in FuelImportBatch.objects.filter(source_type="CLOSING").exclude(status__in=("REJECTED", "FAILED")):
        st = (b.summary or {}).get("stocks_closing") or {}
        if "compteur_terrain_repris" in st or not b.archive_path or not Path(b.archive_path).is_file():
            continue
        oaf = st.get("format") == FILE_LABEL
        recs = []
        for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={SHEET if oaf else CLOSING_SHEET}):
            recs = parse_rows(list(rows)) if oaf else closing_stock_rows(list(rows))[0]
        resolver = SiteResolver()
        by_key = {}
        for r in recs:
            sid = resolver.resolve(r["site"])
            if sid and r.get("date_initiale") and r.get("compteur_terrain_initial_h") is not None:
                by_key[(sid.upper(), r["date_initiale"].isoformat()[:10])] = r["compteur_terrain_initial_h"]
        upd = []
        for o in Obs.objects.filter(batch=b, source_type="CLOSING").only("pk", "site_id", "source_context"):
            ctx = o.source_context if isinstance(o.source_context, dict) else {}
            per = ctx.get("periode") or [None]
            v = by_key.get(((o.site_id or "").upper(), str(per[0] or "")[:10]))
            if v is not None and ctx.get("compteur_terrain_initial_h") != v:
                o.source_context = {**ctx, "compteur_terrain_initial_h": v}
                upd.append(o)
        Obs.objects.bulk_update(upd, ["source_context"], batch_size=1000)
        b.summary = {**(b.summary or {}), "stocks_closing": {**st, "compteur_terrain_repris": len(upd)}}
        b.save(update_fields=["summary"])
        done += 1
    return done
