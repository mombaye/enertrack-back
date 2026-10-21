"""
Bilan stock par site et période EXACTE (services/stock_period_balance.py). Écrit fuel_stock_balance ;
ne certifie jamais (action Fuel Manager dans l'interface).

    python manage.py compute_stock_balances --debut 2026-10-01 --fin 2026-10-12
    python manage.py compute_stock_balances --debut 2026-10-02T00:00 --fin 2026-10-20T23:59 --site DKR_0001
    python manage.py compute_stock_balances --debut 2026-10-01 --fin 2026-10-31 --exemples   # + exemples anonymisés
"""
import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services.stock_period_balance import BalanceError, compute_balances
from fuel_tracking.views_balance import parse_bound


def _anon(r: dict, i: int) -> dict:
    keep = ("observation_start", "observation_end", "stock_initial_l", "source_stock_initial", "date_stock_initial", "stock_final_l",
            "source_stock_final", "date_stock_final", "truck_l", "carte_total_l", "transferts_entrants_l", "transferts_sortants_l",
            "livraisons_externes_l", "consommation_reelle_l", "statut_bilan", "motifs")
    return {"site": f"SITE_{i:03d} (zone {r.get('zone') or '—'}, {r.get('typologie') or 'typologie inconnue'})",
            **{k: r.get(k) for k in keep}, "nb_mouvements": len(r.get("mouvements") or [])}


class Command(BaseCommand):
    help = "Calcule le bilan stock (stock initial, mouvements, stock final, consommation réelle) par site pour une période exacte."

    def add_arguments(self, parser):
        parser.add_argument("--debut", required=True)
        parser.add_argument("--fin", required=True)
        parser.add_argument("--site", action="append", default=[])
        parser.add_argument("--exemples", action="store_true", help="Affiche un bilan calculable et un incomplet, anonymisés")

    def handle(self, *args, debut, fin, site, exemples, **opts):
        start, end = parse_bound(debut), parse_bound(fin, end=True)
        if not (start and end):
            raise CommandError("Dates invalides (AAAA-MM-JJ ou AAAA-MM-JJTHH:MM).")
        try:
            rows = compute_balances(start, end, site_ids=site or None)
        except BalanceError as e:
            raise CommandError(str(e)) from e
        calc = [r for r in rows if r["consommation_reelle_l"] is not None]
        sources = Counter()
        for r in rows:
            for k in ("source_stock_initial", "source_stock_final"):
                if r[k]:
                    sources[f"{r[k]} ({'initial' if 'initial' in k else 'final'})"] += 1
        out = {"periode": f"{start:%d/%m/%Y %H:%M} → {end:%d/%m/%Y %H:%M}", "sites": len(rows), "calculables": len(calc),
               "incomplets": len(rows) - len(calc), "par_statut": dict(Counter(r["statut_bilan"] for r in rows)),
               "stock_initial_present": sum(1 for r in rows if r["stock_initial_l"] is not None),
               "stock_final_present": sum(1 for r in rows if r["stock_final_l"] is not None),
               "sources_stock_utilisees": dict(sources),
               "conversion_cuve_manquante": sum(1 for r in rows if any("CONVERSION_CUVE_MANQUANTE" in a for a in r["alertes"]))}
        if exemples:
            inc = next((r for r in rows if r["statut_bilan"] == "DONNEES_STOCK_INCOMPLETES"), None)
            out["exemple_calculable"] = _anon(calc[0], 1) if calc else None
            out["exemple_incomplet"] = _anon(inc, 2) if inc else None
        self.stdout.write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
