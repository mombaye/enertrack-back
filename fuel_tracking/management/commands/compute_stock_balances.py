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
    keep = ("observation_start", "observation_end", "stock_initial_l", "source_stock_initial", "date_stock_initial", "age_stock_initial_j",
            "stock_final_l", "source_stock_final", "date_stock_final", "age_stock_final_j", "livraisons_externes_l", "carte_total_l",
            "transferts_entrants_l", "transferts_sortants_l", "consommation_reelle_l", "statut_bilan", "cause_principale", "score_confiance",
            "reserves", "motifs")
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
        modes = Counter()
        tanks_used = {"GEOMETRIQUE": set(), "PROPOSEE": set()}
        for r in rows:
            for k in ("source_stock_initial", "source_stock_final"):
                if r[k]:
                    sources[f"{r[k]} ({'initial' if 'initial' in k else 'final'})"] += 1
            for d in (r["stock_initial_detail"], r["stock_final_detail"]):
                if d.get("mode_valeur"):
                    modes[d["mode_valeur"]] += 1
                det = d.get("detail_estimation") or {}
                if d.get("estimation") and det.get("cuve") is not None:
                    tanks_used["GEOMETRIQUE" if d["mode_valeur"].endswith("GEOMETRIQUE_RESERVE") else "PROPOSEE"].add((r["site_id"], det["cuve"]))
        out = {"periode": f"{start:%d/%m/%Y %H:%M} → {end:%d/%m/%Y %H:%M}", "sites": len(rows),
               "prets_a_certifier": sum(1 for r in rows if r["statut_bilan"] == "PRET_A_CERTIFIER"),
               "estimations_avec_reserve": sum(1 for r in rows if r["statut_bilan"] == "ESTIMATION_AVEC_RESERVE"),
               "non_calculables": sum(1 for r in rows if r["statut_bilan"] == "NON_CALCULABLE"),
               "causes_principales": dict(Counter(r["cause_principale"] for r in rows if r["statut_bilan"] == "NON_CALCULABLE").most_common()),
               "reserves_principales": dict(Counter(x.split(" (")[0].split(" :")[0][:60] for r in rows
                                                    if r["statut_bilan"] == "ESTIMATION_AVEC_RESERVE" for x in r["reserves"]).most_common(8)),
               "stock_initial_present": sum(1 for r in rows if r["stock_initial_l"] is not None),
               "stock_final_present": sum(1 for r in rows if r["stock_final_l"] is not None),
               "sources_stock_utilisees": dict(sources), "nature_des_valeurs_de_stock": dict(modes),
               "cuves_geometriques_estimees_utilisees": len(tanks_used["GEOMETRIQUE"]),
               "cuves_proposees_utilisees": len(tanks_used["PROPOSEE"]),
               "volumes": {k: float(sum((r[k] or 0) for r in rows)) for k in ("livraisons_externes_l", "carte_total_l", "entrees_externes_nettes_l",
                                                                              "transferts_entrants_l", "transferts_sortants_l", "volume_brut_integre_l")}}
        from fuel_tracking.services.stock_period_balance import ponctions_globales

        out["volumes"]["impact_global_ponctions_l"] = ponctions_globales(start, end)["impact_global_l"]  # sur les mouvements : doit valoir 0
        if exemples:
            def pick(status):
                return next((r for r in rows if r["statut_bilan"] == status), None)

            out["exemples"] = {"certifiable": _anon(pick("PRET_A_CERTIFIER"), 1) if pick("PRET_A_CERTIFIER") else None,
                               "avec_reserve": _anon(pick("ESTIMATION_AVEC_RESERVE"), 2) if pick("ESTIMATION_AVEC_RESERVE") else None,
                               "non_calculable": _anon(pick("NON_CALCULABLE"), 3) if pick("NON_CALCULABLE") else None}
        self.stdout.write(json.dumps(out, ensure_ascii=False, indent=2, default=str))
