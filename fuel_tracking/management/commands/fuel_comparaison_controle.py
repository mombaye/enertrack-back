"""
Recette « Suivis Consommations » sur données RÉELLES (LECTURE SEULE, rien n'est certifié ni modifié).

    python manage.py fuel_comparaison_controle --debut 2026-08-01 --fin 2026-08-31 [--nombre 12] [--csv chemin] [--anonymiser]

Affiche : la période exacte commune aux trois consommations, les comptages demandés (sites à consommation réelle calculable, certifiables,
avec réserve, non calculables, comparaisons réel/instrumentée et réel/théorique, principaux motifs bloquants) puis ≥ 10 sites réels couvrant
les situations clés (certifiable, réserve, incomplet/non calculable, CPH indisponible, instrumentée indisponible, conflit ENOC/Closing, ponction).
"""
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import default_period


def _p(r, key):  # une situation → une ligne
    return {
        "CERTIFIABLE": lambda x: x["reel"]["statut"] == "CERTIFIABLE" and x["statut_global"] == "COHERENT",
        "CERTIFIABLE_ECART": lambda x: x["reel"]["statut"] == "CERTIFIABLE",
        "RESERVE": lambda x: x["reel"]["statut"] == "ESTIMATION_AVEC_RESERVE",
        "INCOMPLET": lambda x: x["reel"]["statut"] in ("DONNEES_INCOMPLETES", "NON_CALCULABLE", "A_CONTROLER"),
        "CPH_ABSENT": lambda x: x["reel"]["conso_l"] is not None and not x["theorique"]["disponible"],
        "INSTR_ABSENTE": lambda x: x["reel"]["conso_l"] is not None and not x["instrumentee"]["disponible"],
        "CONFLIT": lambda x: "CONFLIT_ENOC_CLOSING" in x["reel"]["alertes"],
        "PONCTION": lambda x: any("PONCTION" in a or "TRANSFERT" in a for a in x["reel"]["alertes"]) or (x["reel"]["cause"] or "").startswith("TRANSFERT"),
        "ECART_FORT": lambda x: x["statut_global"] == "A_INVESTIGUER",
        "COMPARE_3": lambda x: x["reel"]["conso_l"] is not None and x["instrumentee"]["disponible"] and x["theorique"]["disponible"],
    }[key](r)


SITUATIONS = ["COMPARE_3", "CERTIFIABLE", "CERTIFIABLE_ECART", "RESERVE", "INCOMPLET", "CPH_ABSENT", "INSTR_ABSENTE", "CONFLIT", "PONCTION", "ECART_FORT"]


class Command(BaseCommand):
    help = "Recette Suivis Consommations sur données réelles (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--debut")
        parser.add_argument("--fin")
        parser.add_argument("--nombre", type=int, default=12)
        parser.add_argument("--anonymiser", action="store_true")
        parser.add_argument("--csv", default=None)

    def handle(self, *args, debut=None, fin=None, nombre=12, anonymiser=False, csv=None, **opts):
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
        w(f"PÉRIODE EXACTE (réel stock = instrumentée = CPH) : {per['libelle']}  ({per['jours']} jours)")
        w(f"Bilans : {per['sites_avec_bilan']} · à consommation réelle calculable : {per['sites_calculables']}")
        if not per["bilan_disponible"]:
            raise CommandError("Aucun bilan pour cette période exacte : le défaut de période n'est PAS résolu.")
        w("\nCOMPTAGES")
        w(f"  sites au tableau                             : {k['sites']}")
        w(f"  conso réelle calculable                      : {k['reel']['sites_calculables']}")
        w(f"  certifiables                                 : {k['reel']['sites_certifiables']}")
        w(f"  estimation avec réserve                      : {k['reel']['sites_avec_reserve']}")
        w(f"  non calculables / incomplets / à contrôler   : {k['a_traiter']['sites_non_calculables']}")
        w(f"  conflits ENOC/Closing                        : {k['a_traiter']['conflits_enoc_closing']}")
        w(f"  comparaisons réel vs instrumentée            : {k['ecart_instrumentee']['sites_comparables']}  (écart {k['ecart_instrumentee']['ecart_l']} L, {k['ecart_instrumentee']['ecart_pct']} %)")
        w(f"  comparaisons réel vs théorique               : {k['ecart_theorique']['sites_comparables']}  (écart {k['ecart_theorique']['ecart_l']} L, {k['ecart_theorique']['ecart_pct']} %)")
        w(f"  totaux L : réel {k['reel']['total_l']} · instrumentée {k['instrumentee']['total_l']} · théorique {k['theorique']['total_l']}")
        w("  statuts globaux : " + ", ".join(f"{s}={n}" for s, n in Counter(r["statut_global"] for r in rows).most_common()))
        motifs = Counter((r["reel"]["cause"] or r["reel"]["statut"]) for r in rows if r["reel"]["conso_l"] is None)
        w("  principaux motifs bloquants (conso réelle) : " + ", ".join(f"{m}={n}" for m, n in motifs.most_common(6)))
        w("  couvertures CPH : " + " · ".join(f"{c['label']} {c['numerateur']}/{c['denominateur']}" for c in data["qualite_cph"]))

        chosen, seen = [], set()
        for sit in SITUATIONS:
            for r in rows:
                if r["site_id"] not in seen and _p(r, sit):
                    chosen.append((sit, r)); seen.add(r["site_id"]); break
        for r in rows:  # complète jusqu'au nombre demandé, en privilégiant les sites à 3 valeurs
            if len(chosen) >= nombre:
                break
            if r["site_id"] not in seen and r["reel"]["conso_l"] is not None:
                chosen.append(("COMPLEMENT", r)); seen.add(r["site_id"])
        w(f"\n{len(chosen)} SITES RÉELS CONTRÔLÉS (réel · instrumentée · théorique · écarts instr−réel / théo−réel)")
        manquantes = [s for s in SITUATIONS if s not in {c[0] for c in chosen}]
        for i, (sit, r) in enumerate(chosen, 1):
            sid = f"SITE-{i:02d}" if anonymiser else r["site_id"]
            e1, e2 = r["ecart_instrumentee"], r["ecart_theorique"]
            w(f"- [{sit}] {sid} {'' if anonymiser else (r['site_name'] or '')} | réel {r['reel']['conso_l']} ({r['reel']['statut']}) | instr {r['instrumentee']['conso_l']} "
              f"| théo {r['theorique']['conso_l']} | Δi {e1['l']} ({e1['pct'] if e1['pct'] is not None else e1['pct_na']}) | Δt {e2['l']} "
              f"({e2['pct'] if e2['pct'] is not None else e2['pct_na']}) | {r['statut_global']}")
        if manquantes:
            w("Situations absentes de cette période (aucun site réel) : " + ", ".join(manquantes))
        if len(chosen) < 10:
            w(f"ATTENTION : seulement {len(chosen)} sites contrôlables — le critère « 10 sites réels » n'est pas atteint.")
        if csv:
            name, content, n = C.export_csv("controle_complet", d1, d2, {})
            with open(csv, "w", encoding="utf-8") as fh:
                fh.write(content)
            w(f"\nExport « Contrôle complet » : {csv} ({n} lignes) — même contenu que le téléchargement eGrid.")
