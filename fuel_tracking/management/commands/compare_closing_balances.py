"""
Validation de bout en bout du moteur de bilan contre le Closing mensuel (LECTURE SEULE : rien n'est enregistré ni certifié).

Pour chaque site du Closing, le bilan eGrid est calculé sur la période PROPRE à ce site (date de relevé initiale → finale, stocks du Closing
déjà importés, mouvements = ceux d'eGrid / ENOC) puis comparé à la consommation calculée par l'équipe dans le Closing, et les entrées d'eGrid
sont comparées à la « qtité totale livrée » du Closing (couverture des mouvements).

    python manage.py compare_closing_balances --fichier "/mnt/Suivi_carburant_prod/archive/2026-10/03c4fe46721a3c82_….xlsb"
    python manage.py compare_closing_balances --lot 12 --limite 300 --csv /tmp/comparaison_closing.csv
"""
import csv as csvlib
import json
from collections import Counter
from datetime import datetime, timezone as tz
from pathlib import Path
from statistics import median

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services import stock_import_files as F
from fuel_tracking.services import stock_period_balance as SB
from fuel_tracking.services.stock_file_analysis import CLOSING_SHEET, closing_formula, closing_stock_rows, plausible
from fuel_tracking.services.stock_registry import SiteResolver


def _bounds(d_init, d_final):
    return (datetime(d_init.year, d_init.month, d_init.day, tzinfo=tz.utc), datetime(d_final.year, d_final.month, d_final.day, 23, 59, 59, tzinfo=tz.utc))


class Command(BaseCommand):
    help = "Compare les bilans eGrid au Closing mensuel, site par site (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--fichier", default=None)
        parser.add_argument("--lot", type=int, default=None, help="N° de lot Closing (utilise sa copie d'archive).")
        parser.add_argument("--limite", type=int, default=None, help="Nombre maximal de sites comparés (échantillon).")
        parser.add_argument("--csv", default=None)

    def handle(self, *args, fichier, lot, limite, csv: str | None = None, **opts):
        from fuel_tracking.models import FuelImportBatch

        if lot:
            b = FuelImportBatch.objects.filter(pk=lot, source_type="CLOSING").first()
            if b is None or not b.archive_path:
                raise CommandError(f"Lot Closing #{lot} introuvable ou sans copie d'archive.")
            fichier = b.archive_path
        if not fichier or not Path(fichier).is_file():
            raise CommandError("Indiquer --fichier (chemin existant) ou --lot.")
        data = []
        for _n, rows in F.iter_sheets(Path(fichier).read_bytes(), Path(fichier).name, only={CLOSING_SHEET}):
            data, _ = closing_stock_rows(rows)
        resolver, w = SiteResolver(), self.stdout.write
        todo = []
        for r in data:
            sid = resolver.resolve(r["site"])
            has_stock = (r["stock_initial_l"] or 0) > 0 or (r["stock_final_l"] or 0) > 0 or (r["nb_ge"] or 0) >= 1
            if sid and has_stock and r["stock_initial_l"] is not None and r["stock_final_l"] is not None and r["date_initiale"] and r["date_finale"] \
                    and plausible(r["date_initiale"]) and plausible(r["date_finale"]) and r["date_finale"] > r["date_initiale"]:
                todo.append((sid, r))
        if limite:
            todo = todo[:limite]
        status, causes, out = Counter(), Counter(), []
        for i, (sid, r) in enumerate(todo, start=1):
            start, end = _bounds(r["date_initiale"], r["date_finale"])
            res = next(iter(SB.compute_balances(start, end, site_ids=[sid], persist=False)), None)
            if res is None:
                continue
            calc, ref, coherent = closing_formula(r)
            conso = res["consommation_reelle_l"]
            inflow = float(res["livraisons_externes_l"] + res["carte_total_l"] + res["transferts_entrants_l"])
            status[res["statut_bilan"]] += 1
            if res["statut_bilan"] == "NON_CALCULABLE":
                causes[res["cause_principale"]] += 1
            ok = None if conso is None or ref is None else abs(float(conso) - ref) <= max(20.0, 0.05 * abs(ref))
            out.append({"site_id": sid, "debut": r["date_initiale"].isoformat(), "fin": r["date_finale"].isoformat(),
                        "stock_initial_closing_l": r["stock_initial_l"], "stock_final_closing_l": r["stock_final_l"],
                        "conso_closing_l": None if ref is None else round(ref, 1), "conso_egrid_l": None if conso is None else round(float(conso), 1),
                        "ecart_l": None if conso is None or ref is None else round(float(conso) - ref, 1), "concordant": ok,
                        "livre_closing_l": r["livre_l"], "entrees_egrid_l": round(inflow, 1), "statut": res["statut_bilan"], "score": res["score_confiance"],
                        "cause": res["cause_principale"], "formule_closing_coherente": coherent})
        comp = [o for o in out if o["concordant"] is not None]
        errs = [abs(o["ecart_l"]) for o in comp]
        cov = [o for o in out if o["livre_closing_l"]]
        summary = {
            "sites_compares": len(out), "statuts": dict(status), "causes_non_calculable": dict(causes.most_common()),
            "comparables": len(comp), "concordants_5pct_ou_20L": sum(1 for o in comp if o["concordant"]),
            "ecart_absolu_median_l": round(median(errs), 1) if errs else None,
            "couverture_des_entrees": {
                "sites_avec_livraison_closing": len(cov),
                "egrid_egal_closing": sum(1 for o in cov if abs(o["entrees_egrid_l"] - o["livre_closing_l"]) <= max(20.0, 0.05 * abs(o["livre_closing_l"]))),
                "egrid_inferieur": sum(1 for o in cov if o["entrees_egrid_l"] < o["livre_closing_l"] - max(20.0, 0.05 * abs(o["livre_closing_l"]))),
                "egrid_superieur": sum(1 for o in cov if o["entrees_egrid_l"] > o["livre_closing_l"] + max(20.0, 0.05 * abs(o["livre_closing_l"]))),
                "note": "egrid_inferieur = livraisons du Closing absentes d'ENOC / non intégrées (cause n°1 d'une consommation sous-estimée).",
            },
            "plus_gros_ecarts": sorted([o for o in comp if not o["concordant"]], key=lambda o: -abs(o["ecart_l"]))[:8],
        }
        w(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        if csv:
            with open(csv, "w", newline="", encoding="utf-8-sig") as fh:
                wr = csvlib.DictWriter(fh, fieldnames=list(out[0].keys()) if out else ["site_id"], delimiter=";")
                wr.writeheader()
                wr.writerows(out)
            w(f"CSV : {csv}")
        w("Lecture seule : aucun bilan enregistré, aucune certification.")
