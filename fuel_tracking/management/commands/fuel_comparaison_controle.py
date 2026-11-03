"""
Recette « Suivis Consommations » sur données RÉELLES (LECTURE SEULE, rien n'est certifié ni modifié).

    python manage.py fuel_comparaison_controle --debut 2026-08-01 --fin 2026-08-31 [--nombre 20] [--csv chemin] [--anonymiser]

Affiche : la période exacte, les bilans par statut, la couverture target / CPH / instrumentée / Closing, puis des sites réels couvrant les situations clés
(PRET_A_CERTIFIER, ESTIMATION_AVEC_RESERVE, NON_CALCULABLE, stock à 0, variation négative, Carte Total, Truck, Ponction, livraison Closing absente ENOC,
conversion cm vers litres, target historique à confirmer, CPH complète / partielle).
"""
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import default_period

SITUATIONS = {
    "PRET_A_CERTIFIER": lambda x: x["stock"]["statut"] == C.PRET,
    "ESTIMATION_AVEC_RESERVE": lambda x: x["stock"]["statut"] == C.ESTIMATION,
    "NON_CALCULABLE": lambda x: x["stock"]["statut"] == C.NON_CALCULABLE,
    "VARIATION_NEGATIVE": lambda x: x["stock"]["variation_l"] is not None and x["stock"]["variation_l"] < 0,
    "STOCK_A_ZERO": lambda x: x["stock"]["final_l"] == 0 or x["stock"]["initial_l"] == 0,
    "CARTE_TOTAL": lambda x: (x["stock"]["carte_total_l"] or 0) > 0,
    "TRUCK": lambda x: (x["stock"]["truck_l"] or 0) > 0,
    "PONCTION": lambda x: (x["stock"]["ponctions_entrantes_l"] or 0) > 0 or (x["stock"]["ponctions_sortantes_l"] or 0) > 0,
    "LIVRAISON_CLOSING_ABSENTE_ENOC": lambda x: "LIVRAISON_CLOSING_ABSENTE_ENOC" in x["stock"]["alertes"],
    "CONVERSION_CM": lambda x: "ESTIMATION" == x["stock"]["cause"],
    "TARGET_A_CONFIRMER": lambda x: x["target"]["statut"] == "TARGET_HISTORIQUE_A_CONFIRMER",
    "CPH_COMPLETE": lambda x: x["cph"]["complete_l"] is not None,
    "CPH_PARTIELLE": lambda x: x["cph"]["partielle_l"] is not None,
    "INSTRUMENTEE": lambda x: x["instrumentee"]["disponible"],
    "ECART_CLOSING": lambda x: x["closing"]["ecart_l"] is not None,
}


class Command(BaseCommand):
    help = "Recette Suivis Consommations sur données réelles (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--debut")
        parser.add_argument("--fin")
        parser.add_argument("--nombre", type=int, default=20)
        parser.add_argument("--anonymiser", action="store_true")
        parser.add_argument("--csv", default=None)

    def handle(self, *args, debut=None, fin=None, nombre=20, anonymiser=False, csv=None, **opts):
        if debut and fin:
            d1, d2 = datetime.strptime(debut[:10], "%Y-%m-%d").date(), datetime.strptime(fin[:10], "%Y-%m-%d").date()
        else:
            dflt = default_period()
            if dflt is None:
                raise CommandError("Aucun bilan de stock calculé : lancez compute_stock_balances.")
            d1, d2 = dflt
        data = C.build(d1, d2, {}, with_cph_quality=True)
        rows, per, k = data["rows"], data["periode"], data["kpis"]
        w = self.stdout.write
        w(f"PÉRIODE EXACTE (identique pour toutes les mesures) : {per['libelle']}  ({per['jours']} jours) — règle {per['regle']}")
        w(f"Bilans : {per['sites_avec_bilan']} · calculables : {per['sites_calculables']}")
        if not per["bilan_disponible"]:
            raise CommandError("Aucun bilan pour cette période exacte.")
        b, t = k["bilans"], k["target"]
        w("\nBILANS PAR STATUT")
        w(f"  PRET_A_CERTIFIER {b['prets']} · ESTIMATION_AVEC_RESERVE {b['avec_reserve']} · VARIATION_CALCULEE {b['variation_calculee']} · "
          f"NON_CALCULABLE {b['non_calculables']} · CONSOMMATION_REELLE_CERTIFIEE {b['certifies']}")
        w("\nMESURES (somme L · sites)")
        for key, lab in (("variation", "Variation de stock"), ("certifiee", "Consommation réelle certifiée"), ("closing", "Consommation Closing"),
                         ("instrumentee", "Instrumentée Snowflake"), ("cph_complete", "CPH complète"), ("cph_partielle", "CPH partielle")):
            w(f"  {lab:34s}: {k[key]['total_l']} L · {k[key]['sites']} sites")
        w(f"  variations négatives : {k['variation']['negatives']}")
        w("\nTARGET FUEL")
        w(f"  sites avec target {t['sites_avec_target']} · sans target {t['sites_sans_target']} · couverture {t['couverture_pct']} % · à confirmer {t['a_confirmer']}")
        w(f"  target total période {t['total_periode_l']} L · target moyen/site {t['target_moyen_site_l']} L · variation moyenne/site {t['variation_moyenne_site_l']} L")
        w(f"  écart global variation vs target : {t['ecart_global_l']} L ({t['ecart_global_pct']} %) sur {t['sites_comparables']} sites comparables")
        w(f"\nCOUVERTURE CPH complète : {k['couverture_cph_pct']} %")
        w("  couvertures du moteur CPH : " + " · ".join(f"{c['label']} {c['numerateur']}/{c['denominateur']}" for c in data["qualite_cph"]))
        w("  statuts target : " + ", ".join(f"{s}={n}" for s, n in Counter(r["target"]["statut"] for r in rows).most_common()))
        w("  causes de blocage : " + ", ".join(f"{s}={n}" for s, n in Counter(r["stock"]["cause"] for r in rows if r["stock"]["statut"] == C.NON_CALCULABLE).most_common(6)))

        chosen, seen = [], set()
        for name, fn in SITUATIONS.items():
            for r in rows:
                if r["site_id"] not in seen and fn(r):
                    chosen.append((name, r)); seen.add(r["site_id"]); break
        missing = [n for n in SITUATIONS if n not in {c[0] for c in chosen}]
        w(f"\n{len(chosen)} SITES RÉELS (situation · variation · Closing · target · instrumentée · CPH complète/partielle · statut)")
        for i, (name, r) in enumerate(chosen, 1):
            sid = f"SITE-{i:02d}" if anonymiser else r["site_id"]
            s = r["stock"]
            w(f"- [{name}] {sid} | {s['statut']} | variation {s['variation_l']} | closing {r['closing']['conso_l']} (Δ {r['closing']['ecart_l']}) | "
              f"target {r['target']['periode_l']} ({r['target']['statut']}, Δ {r['target']['ecart_l']}) | instr {r['instrumentee']['conso_l']} | "
              f"CPH {r['cph']['complete_l']}/{r['cph']['partielle_l']} | certifiée {r['certification']['valeur_l']}")
        if missing:
            w("Situations absentes de cette période (aucun site réel) : " + ", ".join(missing))
        if csv:
            name, content, n = C.export_csv("controle_complet", d1, d2, {})
            with open(csv, "w", encoding="utf-8") as fh:
                fh.write(content)
            w(f"\nExport « Contrôle complet » : {csv} ({n} lignes).")
