"""
Explication de l'écart entre la consommation du fichier Closing (« initiale ») et la variation de stock calculée par la plateforme.

    écart = variation de stock − consommation Closing
          = (stock initial eGrid − stock initial Closing)            ← autre relevé / autre date de relevé
          − (stock final eGrid − stock final Closing)                ← idem à la borne finale
          + (mouvements retenus par eGrid − mouvements du Closing)   ← livraisons, Carte Total, Ponctions, vols… absents ou différents
          + (calcul interne du Closing − consommation déclarée)      ← formule du fichier Closing incohérente

La décomposition est EXACTE quand les stocks et mouvements du Closing sont connus ; sinon la part non quantifiable est signalée (jamais inventée).
Les causes qualitatives (relevé éloigné de la borne, estimation de cuve, mouvements exclus, conflit ENOC/Closing) complètent l'explication.
"""
from __future__ import annotations


def _closing_candidate(cands: list[dict] | None) -> dict | None:
    for c in cands or []:
        if c.get("source") == "CLOSING" and c.get("litres") is not None:
            return c
    return None


def _num(v):
    return None if v is None else float(v)


def explain(b, variation: float | None, closing_conso: float | None, ref: dict | None) -> dict:
    """b : bilan (FuelStockBalance), variation : variation de stock (L), closing_conso : consommation du fichier Closing (L), ref : contexte Closing."""
    out = {"ecart_l": None, "items": [], "explique_l": None, "residuel_l": None, "quantifiable": False}
    if b is None or variation is None or closing_conso is None:
        return out
    gap = round(variation - closing_conso, 1)
    out["ecart_l"] = gap
    items: list[dict] = []
    cands = b.candidats or {}
    ini_c, fin_c = _closing_candidate(cands.get("initial")), _closing_candidate(cands.get("final"))
    ini_e, fin_e = _num(b.stock_initial_l), _num(b.stock_final_l)
    quantified = 0.0
    all_known = True

    # 1-2. Stocks aux deux bornes : autre source ou autre date que le Closing
    if ini_e is not None and ini_c is not None:
        d = round(ini_e - ini_c["litres"], 1)
        if abs(d) >= 0.5:
            items.append({"code": "STOCK_INITIAL_DIFFERENT", "label": "Stock initial différent du Closing", "montant_l": d,
                          "detail": f"eGrid : {ini_e:.0f} L ({b.source_stock_initial}, {str(b.date_stock_initial)[:10]}) · Closing : {ini_c['litres']:.0f} L ({str(ini_c.get('date'))[:10]})."})
            quantified += d
    else:
        all_known = all_known and ini_e is None
    if fin_e is not None and fin_c is not None:
        d = round(-(fin_e - fin_c["litres"]), 1)
        if abs(d) >= 0.5:
            items.append({"code": "STOCK_FINAL_DIFFERENT", "label": "Stock final différent du Closing", "montant_l": d,
                          "detail": f"eGrid : {fin_e:.0f} L ({b.source_stock_final}, {str(b.date_stock_final)[:10]}) · Closing : {fin_c['litres']:.0f} L ({str(fin_c.get('date'))[:10]})."})
            quantified += d
    else:
        all_known = all_known and fin_e is None

    # 3. Mouvements retenus par eGrid vs mouvements du Closing
    if ref and ref.get("mouvements_nets_closing_l") is not None:
        e = lambda x: float(x or 0)  # noqa: E731
        egrid_net = (e(b.livraisons_externes_l) + e(b.carte_total_l) + e(b.transferts_entrants_l) + e(b.rajouts_l)
                     - e(b.transferts_sortants_l) - e(b.retraits_l) - e(b.vols_l) - e(b.vidanges_l))
        d = round(egrid_net - float(ref["mouvements_nets_closing_l"]), 1)
        if abs(d) >= 0.5:
            items.append({"code": "MOUVEMENTS_DIFFERENTS", "label": "Mouvements différents du Closing", "montant_l": d,
                          "detail": f"Mouvements nets retenus par eGrid : {egrid_net:.0f} L · Closing : {float(ref['mouvements_nets_closing_l']):.0f} L"
                                    f" (entrées {float(ref.get('entrees_closing_l') or 0):.0f} L au Closing contre {e(b.livraisons_externes_l) + e(b.carte_total_l) + e(b.transferts_entrants_l) + e(b.rajouts_l):.0f} L"
                                    f" côté eGrid ; décision : {ref.get('decision') or '—'})."})
            quantified += d
        # 4. Formule interne du fichier Closing
        if ini_c is not None and fin_c is not None:
            calc = ini_c["litres"] + float(ref["mouvements_nets_closing_l"]) - fin_c["litres"]
            d = round(calc - closing_conso, 1)
            if abs(d) >= 0.5:
                items.append({"code": "FORMULE_CLOSING_INCOHERENTE", "label": "Formule du fichier Closing incohérente", "montant_l": d,
                              "detail": f"Le Closing déclare {closing_conso:.0f} L mais son propre calcul (stock initial + mouvements − stock final) donne {calc:.0f} L."})
                quantified += d
        else:
            all_known = False
    else:
        all_known = False

    # Causes qualitatives (non chiffrées)
    for side, age, src in (("initial", b.age_stock_initial_j, b.source_stock_initial), ("final", b.age_stock_final_j, b.source_stock_final)):
        if age:
            items.append({"code": "RELEVE_ELOIGNE_BORNE", "label": f"Stock {side} relevé {age} j avant la borne", "montant_l": None,
                          "detail": f"Source {src} : le stock {side} n'est pas daté du jour exact de la borne de période ; la consommation entre le relevé et la borne n'est pas observée."})
    if b.estimation:
        items.append({"code": "CONVERSION_ESTIMEE", "label": "Stock estimé (conversion cm → litres avec réserve)", "montant_l": None,
                      "detail": "Cuve sans plaque validée (estimation géométrique ou cuve proposée) : l'incertitude de conversion peut expliquer un écart."})
    excl = [m for m in (b.mouvements_exclus or []) if m.get("quantite_l")]
    if excl:
        tot = round(sum(float(m.get("quantite_l") or 0) for m in excl), 1)
        items.append({"code": "MOUVEMENTS_EXCLUS", "label": f"{len(excl)} mouvement(s) ENOC non retenu(s)", "montant_l": None,
                      "detail": f"{tot:.0f} L de mouvements exclus (statut non vérifiable, hors période ou remplacés) — voir le détail du bilan."})
    if "CONFLIT_ENOC_CLOSING" in " ".join(b.alertes or []):
        items.append({"code": "CONFLIT_ENOC_CLOSING", "label": "Conflit ENOC / Closing sur les mouvements", "montant_l": None,
                      "detail": "Les deux valeurs sont conservées ; arbitrage du Fuel Manager requis."})

    out["items"] = items
    out["quantifiable"] = all_known
    if all_known:
        out["explique_l"] = round(quantified, 1)
        out["residuel_l"] = round(gap - quantified, 1)
    return out
