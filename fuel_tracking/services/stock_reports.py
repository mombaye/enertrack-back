# fuel_tracking/services/stock_reports.py
"""
Rapports téléchargeables (CSV) de la validation terrain — accessibles depuis eGrid (Imports stock / Priorités de fiabilisation), jamais dans /tmp.
Tous en LECTURE SEULE : aucune donnée n'est modifiée, aucune certification.

  lignes_rejetees              lignes de fichiers rejetées à l'import (motifs)
  validation_10_sites          tableau de validation sur ~10 sites réels d'une période calculée
  livraisons_closing_absentes  livraisons du Closing absentes d'ENOC (complétées avec réserve)
  ponctions_sans_source        Ponctions ENOC dont le site source est absent / inconnu (non intégrées)
  conflits_enoc_closing        mouvements ENOC et Closing en conflit (les deux valeurs)
  cuves_prioritaires           cuves à fiabiliser, classées par impact
  closing_formule_incoherente  lignes du Closing dont la formule interne est incohérente
  consommations_negatives      bilans dont la consommation calculée est négative (mouvement manquant ou relevé erroné)
  stocks_identiques_suspects   bilans dont les stocks initial et final sont identiques sans mouvement (relevé reporté / capteur figé ?)
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, time

from django.utils import timezone

from fuel_tracking.services.stock_period_balance import alert_codes_of


class ReportError(ValueError):
    pass


REPORTS = {
    "lignes_rejetees": {"label": "Lignes rejetées des imports", "periode": False, "lot": True,
                        "description": "Toutes les lignes de fichiers rejetées, avec feuille, ligne, site brut et motif."},
    "validation_10_sites": {"label": "Tableau de validation (10 sites réels)", "periode": True, "lot": False,
                            "description": "Sites réels de la période couvrant litre direct, cm/cuve validée, cuve géométrique, cuve proposée, Truck, Carte Total, Ponction…"},
    "livraisons_closing_absentes": {"label": "Livraisons du Closing absentes d'ENOC", "periode": True, "lot": False,
                                    "description": "Bilans complétés avec la livraison du Closing (estimation avec réserve) : à saisir dans ENOC."},
    "ponctions_sans_source": {"label": "Ponctions ENOC sans site source", "periode": False, "lot": False,
                              "description": "Ponctions validées dont le site source est absent ou inconnu : non intégrables tant que la source n'est pas renseignée."},
    "conflits_enoc_closing": {"label": "Conflits ENOC / Closing", "periode": True, "lot": False,
                              "description": "Bilans où ENOC et le Closing divergent : les deux valeurs sont affichées, rien n'est écrasé."},
    "cuves_prioritaires": {"label": "Cuves prioritaires à fiabiliser", "periode": False, "lot": False,
                           "description": "Cuves sans plaque, proposées, incohérentes… classées par impact (sites prioritaires du métier en tête)."},
    "consommations_negatives": {"label": "Consommations négatives à contrôler", "periode": True, "lot": False,
                                "description": "Stock final plus haut que stock initial + entrées : livraison manquante, relevé erroné ou sortie non saisie. Aucune consommation n'est affichée."},
    "stocks_identiques_suspects": {"label": "Stocks identiques sans mouvement (suspects)", "periode": True, "lot": False,
                                   "description": "Stock initial = stock final sans aucun mouvement : relevé reporté ou capteur figé ? À faire vérifier sur site."},
    "closing_formule_incoherente": {"label": "Lignes du Closing à formule incohérente", "periode": False, "lot": True,
                                    "description": "Stock initial + livré + rajouts/retraits − volé − vidangé − stock final ≠ consommation du Closing : à faire relire."},
}


def parse_day(v, end=False):
    if not v:
        return None
    try:
        d = datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        raise ReportError(f"Date invalide : {v!r} (format AAAA-MM-JJ).")
    return timezone.make_aware(datetime.combine(d, time(23, 59, 59) if end else time.min), timezone.get_current_timezone())


def _period(params):
    start, end = parse_day(params.get("debut")), parse_day(params.get("fin"), end=True)
    if not (start and end):
        raise ReportError("Ce rapport demande une période (debut et fin, AAAA-MM-JJ) déjà calculée dans le bilan de stock.")
    return start, end


def _balances(start, end):
    from fuel_tracking.models import FuelStockBalance as B

    rows = list(B.objects.filter(observation_start=start, observation_end=end).order_by("site_id"))
    if not rows:
        raise ReportError("Aucun bilan calculé pour cette période exacte : calculer d'abord le bilan (Stock › Bilan de stock).")
    return rows


def _f(v):
    return None if v is None else float(v)


def _label(start, end):
    return f"{start:%Y-%m-%d}_{end:%Y-%m-%d}"


# ── Tableau de validation (profils couverts) ─────────────────────────────────────────────────────────────────────
ACTIONS = {
    "DONNEES_STOCK_INCOMPLETES": "Obtenir / importer le relevé initial ou final manquant.",
    "CM_SANS_CUVE": "Valider ou compléter la cuve du site (relevés en cm sans cuve exploitable).",
    "STOCK_TROP_ANCIEN": "Obtenir un relevé terrain plus proche de la borne.",
    "MOUVEMENTS_A_CONTROLER": "Contrôler / compléter le mouvement ENOC concerné (lecture seule dans eGrid).",
    "TRANSFERT_DESEQUILIBRE": "Vérifier la Ponction (deux écritures liées, somme nulle).",
    "CONSO_NEGATIVE": "Vérifier relevés et mouvements manquants.",
    "SITE_NON_RECONNU": "Rattacher le site dans Gestion des sites.",
    "RELEVES_INCOHERENTS": "Obtenir un second relevé postérieur.",
    "SCORE_INSUFFISANT": "Compléter les données pour dépasser le seuil de confiance.",
    "ESTIMATION": "Relever la plaque / valider la cuve pour rendre le bilan certifiable.",
    "RESERVES": "Lever les réserves (données manquantes) puis certifier.",
    "CONFLIT_ENOC_CLOSING": "Arbitrer ENOC vs Closing (conflit supérieur au seuil de contrôle).",
}
ALERT_ACTIONS = {
    "LIVRAISON_CLOSING_ABSENTE_ENOC": "Compléter dans ENOC la/les livraison(s) du Closing absente(s) : le bilan pourra alors être certifié.",
    "SORTIE_CLOSING_ABSENTE_ENOC": "Saisir dans ENOC le retrait / vol / vidange présent au Closing.",
    "CONFLIT_ENOC_CLOSING": "Arbitrer ENOC vs Closing (les deux valeurs sont conservées).",
}
WANTED = ["stock_litre_direct", "cm_cuve_validee", "cuve_geometrique_sans_plaque", "cuve_proposee", "non_calculable",
          "livraison_truck", "carte_total", "ponction", "mouvement_a_controler"]


def action_for(b) -> str:
    codes = alert_codes_of(b.alertes)
    for code, action in ALERT_ACTIONS.items():
        if code in codes:
            return action
    if b.cause_principale in ACTIONS:
        return ACTIONS[b.cause_principale]
    if b.statut_bilan == "PRET_A_CERTIFIER":
        return "Certification manuelle par le Fuel Manager après contrôle des données."
    return "Compléter les données du site (cause non référencée : voir les motifs)."


def profils(b) -> list[str]:
    modes = {m for m in ((b.stock_initial_detail or {}).get("mode_valeur"), (b.stock_final_detail or {}).get("mode_valeur")) if m}
    out = []
    if b.statut_bilan == "NON_CALCULABLE":
        out.append("non_calculable")
    if modes and modes <= {"LITRE_DIRECT"}:
        out.append("stock_litre_direct")
    if modes and modes <= {"LITRE_DIRECT", "CM_CONVERTI_CUVE_VALIDEE"} and "CM_CONVERTI_CUVE_VALIDEE" in modes:
        out.append("cm_cuve_validee")
    if any(m.endswith("GEOMETRIQUE_RESERVE") for m in modes):
        out.append("cuve_geometrique_sans_plaque")
    if any(m.endswith("PROPOSEE_RESERVE") for m in modes):
        out.append("cuve_proposee")
    if b.livraisons_externes_l:
        out.append("livraison_truck")
    if b.carte_total_l:
        out.append("carte_total")
    if b.transferts_entrants_l or b.transferts_sortants_l:
        out.append("ponction")
    if b.cause_principale == "MOUVEMENTS_A_CONTROLER" or any("contrôler" in (m.get("motif") or "") for m in (b.mouvements_exclus or [])):
        out.append("mouvement_a_controler")
    return out


def pick(rows: list, n: int) -> list:
    """Sélection gloutonne : à chaque pas, le site qui ajoute le plus de profils non encore couverts (puis score le plus haut)."""
    cand = [(b, set(profils(b))) for b in rows]
    covered, chosen = set(), []
    while cand and len(chosen) < n:
        cand.sort(key=lambda x: (-len((x[1] & set(WANTED)) - covered), -(x[0].score_confiance or 0), x[0].site_id))
        b, p = cand.pop(0)
        chosen.append((b, sorted(p)))
        covered |= p
    return chosen


VALIDATION_COLUMNS = ["site_id", "periode", "source_stock_initial", "stock_initial_l", "source_stock_final", "stock_final_l", "truck_l", "carte_total_l",
                      "transferts_entrants_l", "transferts_sortants_l", "conso_reelle_estimee_l", "statut", "cause", "score", "reserves", "motifs",
                      "action_attendue", "profil_de_test"]


def validation_table(start, end, n=10, anonymiser=False) -> tuple[list[dict], list[str], list[str]]:
    """(lignes, profils couverts, profils non trouvés)."""
    chosen = pick(_balances(start, end), n)
    table = []
    for i, (b, p) in enumerate(chosen, start=1):
        table.append({
            "site_id": f"SITE_{i:03d}" if anonymiser else b.site_id, "periode": f"{start:%d/%m/%Y} → {end:%d/%m/%Y}",
            "source_stock_initial": b.source_stock_initial, "stock_initial_l": _f(b.stock_initial_l),
            "source_stock_final": b.source_stock_final, "stock_final_l": _f(b.stock_final_l),
            "truck_l": _f(b.livraisons_externes_l), "carte_total_l": _f(b.carte_total_l),
            "transferts_entrants_l": _f(b.transferts_entrants_l), "transferts_sortants_l": _f(b.transferts_sortants_l),
            "conso_reelle_estimee_l": _f(b.consommation_reelle_l), "statut": b.statut_bilan, "cause": b.cause_principale, "score": b.score_confiance,
            "reserves": " | ".join((b.reserves or [])[:3]) or "aucune", "motifs": " | ".join((b.motifs or [])[:2]),
            "action_attendue": action_for(b), "profil_de_test": ", ".join(p),
        })
    covered = {x for _, p in chosen for x in p}
    return table, sorted(covered), [x for x in WANTED if x not in covered]


# ── Rapports ─────────────────────────────────────────────────────────────────────────────────────────────────────
def _rows_lignes_rejetees(params):
    from fuel_tracking.models import FuelImportRow
    from fuel_tracking.services.stock_file_analysis import IMPORT_SOURCE_LABEL

    qs = FuelImportRow.objects.filter(validation_status="REJECTED").select_related("batch").order_by("batch_id", "sheet_name", "row_number")
    if params.get("lot"):
        qs = qs.filter(batch_id=params["lot"])
    cols = ["lot", "source", "fichier", "feuille", "ligne", "site_brut", "site_reconnu", "date", "codes", "motifs"]
    rows = [{"lot": r.batch_id, "source": IMPORT_SOURCE_LABEL.get(r.batch.source_type, r.batch.source_type), "fichier": r.batch.file_name,
             "feuille": r.sheet_name, "ligne": r.row_number, "site_brut": r.site_id_raw, "site_reconnu": r.site_id_resolved,
             "date": r.event_datetime.date().isoformat() if r.event_datetime else None,
             "codes": ", ".join(m.get("code", "") for m in (r.messages or [])), "motifs": " | ".join(m.get("message", "") for m in (r.messages or []))}
            for r in qs.iterator()]
    return "lignes_rejetees" + (f"_lot{params['lot']}" if params.get("lot") else ""), cols, rows


def _rows_validation(params):
    start, end = _period(params)
    table, _covered, _missing = validation_table(start, end, int(params.get("n") or 10), params.get("anonymiser") in ("1", "true", True))
    return f"validation_10_sites_{_label(start, end)}", VALIDATION_COLUMNS, table


def _closing_rows(params, code, name, cols_extra=()):
    start, end = _period(params)
    rows = []
    for b in _balances(start, end):
        if code not in alert_codes_of(b.alertes):
            continue
        ref = (b.stock_initial_detail or {}).get("reference_closing") or {}
        rows.append({
            "site_id": b.site_id, "nom": b.site_name, "zone": b.zone, "periode": f"{start:%d/%m/%Y} → {end:%d/%m/%Y}",
            "entrees_closing_l": ref.get("entrees_closing_l"), "entrees_egrid_enoc_l": ref.get("entrees_egrid_l"), "ecart_entrees_closing_moins_enoc_l": ref.get("ecart_entrees_l"),
            "sorties_closing_l": ref.get("sorties_closing_l"), "sorties_egrid_enoc_l": ref.get("sorties_egrid_l"), "ecart_sorties_closing_moins_enoc_l": ref.get("ecart_sorties_l"),
            "conso_closing_l": ref.get("conso_closing_l"), "conso_egrid_l": _f(b.consommation_reelle_l), "decision": ref.get("decision"),
            "lot_closing": ref.get("lot"), "fichier_closing": ref.get("fichier"), "statut": b.statut_bilan, "cause": b.cause_principale,
            "score": b.score_confiance, "alerte": next((a for a in b.alertes if a.startswith(code)), None), "action_attendue": action_for(b),
        })
    cols = ["site_id", "nom", "zone", "periode", "entrees_closing_l", "entrees_egrid_enoc_l", "ecart_entrees_closing_moins_enoc_l", "sorties_closing_l", "sorties_egrid_enoc_l",
            "ecart_sorties_closing_moins_enoc_l", "conso_closing_l", "conso_egrid_l", "decision", "lot_closing", "fichier_closing", "statut", "cause", "score", "alerte", "action_attendue"]
    key = "ecart_entrees_closing_moins_enoc_l" if code == "LIVRAISON_CLOSING_ABSENTE_ENOC" else "ecart_sorties_closing_moins_enoc_l"
    rows.sort(key=lambda r: -abs(r.get(key) or 0))
    return f"{name}_{_label(start, end)}", cols, rows


def _rows_ponctions_sans_source(params):
    from fuel_tracking.models import FuelEnocQualifiedMovement as Q

    qs = Q.objects.filter(type_family="PONCTION", non_integration_cause="SITE_SOURCE_ABSENT").order_by("-approved_quantity_l", "enoc_reference")
    cols = ["enoc_reference", "site_destinataire", "site_destinataire_reconnu", "site_source_brut", "quantite_approuvee_l", "statut_enoc", "statut_metier",
            "date_validation", "date_operation", "cause", "action_attendue"]
    rows = [{"enoc_reference": q.enoc_reference, "site_destinataire": q.site_id, "site_destinataire_reconnu": q.site_id_resolved,
             "site_source_brut": q.ponction_source_site_id, "quantite_approuvee_l": _f(q.approved_quantity_l), "statut_enoc": q.raw_status,
             "statut_metier": q.business_status, "date_validation": q.approved_at.date().isoformat() if q.approved_at else None,
             "date_operation": q.actual_operation_at.date().isoformat() if q.actual_operation_at else None, "cause": q.non_integration_cause,
             "action_attendue": "Renseigner le site source de la Ponction dans ENOC (eGrid lit ENOC, ne le modifie pas)."} for q in qs.iterator()]
    return "ponctions_enoc_sans_site_source", cols, rows


def _rows_cuves(params):
    from fuel_tracking.views_mvp import tank_rows

    rows, _c, _u, _last, _min = tank_rows(params.get("site"), params.get("categorie"), params.get("usage"))
    cols = ["priorite_metier", "site_id", "cuve", "statut", "categories", "forme", "cotes_cm", "plaque_l", "geometrie_l", "ecart_plaque_pct", "score_compatibilite",
            "usage", "bilans_impactes", "releves_bloques", "releves_estimes", "action_attendue"]
    out = [{"priorite_metier": "oui" if r["prioritaire_demande"] else "", "site_id": r["site_id"], "cuve": r["cuve"], "statut": r["statut"],
            "categories": ", ".join(r["categories"]), "forme": r["forme"], "cotes_cm": " / ".join("" if x is None else f"{x:g}" for x in r["cotes_cm"]),
            "plaque_l": r["plaque_l"], "geometrie_l": r["geometrie_l"], "ecart_plaque_pct": r["ecart_plaque_pct"], "score_compatibilite": r["score_compatibilite"],
            "usage": r["usage"], "bilans_impactes": r["bilans_impactes"], "releves_bloques": r["releves_bloques"], "releves_estimes": r["releves_estimes"],
            "action_attendue": r["action"]} for r in rows if r["categories"] != ["VALIDEE"] or r["prioritaire_demande"]]
    return "cuves_prioritaires", cols, out


def _rows_closing_incoherent(params):
    from fuel_tracking.models import FuelImportBatch, FuelStockObservation as O

    qs = O.objects.filter(source_type="CLOSING").exclude(review_flags=[]).order_by("site_id", "observation_datetime")
    if params.get("lot"):
        qs = qs.filter(batch_id=params["lot"])
    groups: dict = {}
    for o in qs:
        if not any(f.get("code") == "CLOSING_FORMULE_INCOHERENTE" for f in (o.review_flags or [])):
            continue
        ctx = o.source_context or {}
        groups.setdefault((o.site_id, tuple(ctx.get("periode") or ())), []).append((o, ctx))
    batches = dict(FuelImportBatch.objects.filter(pk__in={o.batch_id for g in groups.values() for o, _ in g if o.batch_id}).values_list("pk", "file_name"))
    cols = ["site_id", "debut", "fin", "stock_initial_l", "stock_final_l", "livre_l", "rajout_retrait_l", "vole_l", "vidange_l", "conso_closing_l",
            "conso_calculee_l", "ecart_l", "lot", "fichier", "statut_stocks", "action_attendue"]
    rows = []
    for (sid, per), g in groups.items():
        ini = next((o for o, _ in g if per and o.observation_datetime.date().isoformat() == per[0]), None)
        fin = next((o for o, _ in g if per and o.observation_datetime.date().isoformat() == per[1]), None)
        ctx = g[0][1]
        si, sf = _f(ini.level_liters) if ini else None, _f(fin.level_liters) if fin else None
        calc = None if si is None or sf is None else round(si + (ctx.get("livre_l") or 0) + (ctx.get("rajout_retrait_l") or 0) - (ctx.get("vole_l") or 0)
                                                           - (ctx.get("vidange_l") or 0) - sf, 1)
        ref = ctx.get("conso_closing_l")
        lot = g[0][0].batch_id
        rows.append({"site_id": sid, "debut": per[0] if per else None, "fin": per[1] if per else None, "stock_initial_l": si, "stock_final_l": sf,
                     "livre_l": ctx.get("livre_l"), "rajout_retrait_l": ctx.get("rajout_retrait_l"), "vole_l": ctx.get("vole_l"), "vidange_l": ctx.get("vidange_l"),
                     "conso_closing_l": None if ref is None else round(ref, 1), "conso_calculee_l": calc,
                     "ecart_l": None if calc is None or ref is None else round(calc - ref, 1), "lot": lot, "fichier": batches.get(lot),
                     "statut_stocks": "à valider (non utilisés par le moteur)", "action_attendue": "Faire relire la ligne du Closing (stocks, livré, volé, vidangé)."})
    rows.sort(key=lambda r: -abs(r["ecart_l"] or 0))
    return "closing_lignes_formule_incoherente" + (f"_lot{params['lot']}" if params.get("lot") else ""), cols, rows


def _rows_conso_negative(params):
    start, end = _period(params)
    cols = ["site_id", "nom", "zone", "periode", "source_stock_initial", "date_stock_initial", "stock_initial_l", "source_stock_final", "date_stock_final", "stock_final_l",
            "entrees_l", "sorties_l", "conso_brute_calculee_l", "nb_mouvements", "statut", "cause", "score", "motif", "action_attendue"]
    rows = []
    for b in _balances(start, end):
        if b.cause_principale != "CONSO_NEGATIVE":
            continue
        z = lambda v: float(v or 0)  # noqa: E731
        ent = z(b.livraisons_externes_l) + z(b.carte_total_l) + z(b.transferts_entrants_l) + z(b.rajouts_l)
        sor = z(b.transferts_sortants_l) + z(b.retraits_l) + z(b.vols_l) + z(b.vidanges_l)
        brute = None if b.stock_initial_l is None or b.stock_final_l is None else round(z(b.stock_initial_l) + ent - sor - z(b.stock_final_l), 1)
        rows.append({"site_id": b.site_id, "nom": b.site_name, "zone": b.zone, "periode": f"{start:%d/%m/%Y} → {end:%d/%m/%Y}",
                     "source_stock_initial": b.source_stock_initial, "date_stock_initial": b.date_stock_initial.date().isoformat() if b.date_stock_initial else None,
                     "stock_initial_l": _f(b.stock_initial_l), "source_stock_final": b.source_stock_final,
                     "date_stock_final": b.date_stock_final.date().isoformat() if b.date_stock_final else None, "stock_final_l": _f(b.stock_final_l),
                     "entrees_l": round(ent, 1), "sorties_l": round(sor, 1), "conso_brute_calculee_l": brute, "nb_mouvements": len(b.mouvements or []),
                     "statut": b.statut_bilan, "cause": b.cause_principale, "score": b.score_confiance,
                     "motif": next((m for m in (b.motifs or []) if "négative" in m), None),
                     "action_attendue": "Chercher la livraison / le relevé manquant (stock final > stock initial + entrées), puis saisir dans ENOC ou corriger le relevé."})
    rows.sort(key=lambda r: (r["conso_brute_calculee_l"] if r["conso_brute_calculee_l"] is not None else 0))
    return f"consommations_negatives_{_label(start, end)}", cols, rows


def _rows_stocks_identiques(params):
    start, end = _period(params)
    cols = ["site_id", "nom", "zone", "periode", "source_stock_initial", "date_stock_initial", "source_stock_final", "date_stock_final", "stock_l", "nb_mouvements",
            "statut", "score", "action_attendue"]
    rows = []
    for b in _balances(start, end):
        if not any("identiques sans aucun mouvement" in m for m in (b.alertes or []) + (b.motifs or [])):
            continue
        rows.append({"site_id": b.site_id, "nom": b.site_name, "zone": b.zone, "periode": f"{start:%d/%m/%Y} → {end:%d/%m/%Y}",
                     "source_stock_initial": b.source_stock_initial, "date_stock_initial": b.date_stock_initial.date().isoformat() if b.date_stock_initial else None,
                     "source_stock_final": b.source_stock_final, "date_stock_final": b.date_stock_final.date().isoformat() if b.date_stock_final else None,
                     "stock_l": _f(b.stock_initial_l), "nb_mouvements": len(b.mouvements or []), "statut": b.statut_bilan, "score": b.score_confiance,
                     "action_attendue": "Faire vérifier le niveau sur site (relevé reporté ou capteur figé ?) avant toute certification."})
    rows.sort(key=lambda r: -(r["stock_l"] or 0))
    return f"stocks_identiques_suspects_{_label(start, end)}", cols, rows


BUILDERS = {
    "consommations_negatives": _rows_conso_negative,
    "stocks_identiques_suspects": _rows_stocks_identiques,
    "lignes_rejetees": _rows_lignes_rejetees,
    "validation_10_sites": _rows_validation,
    "livraisons_closing_absentes": lambda p: _closing_rows(p, "LIVRAISON_CLOSING_ABSENTE_ENOC", "livraisons_closing_absentes_enoc"),
    "ponctions_sans_source": _rows_ponctions_sans_source,
    "conflits_enoc_closing": lambda p: _closing_rows(p, "CONFLIT_ENOC_CLOSING", "conflits_enoc_closing"),
    "cuves_prioritaires": _rows_cuves,
    "closing_formule_incoherente": _rows_closing_incoherent,
}


def build_report(kind: str, params: dict) -> tuple[str, list[str], list[dict]]:
    if kind not in BUILDERS:
        raise ReportError(f"Rapport inconnu : {kind}.")
    return BUILDERS[kind](params)


def to_csv(columns: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return "﻿" + buf.getvalue()  # BOM : ouverture directe dans Excel
