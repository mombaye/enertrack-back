"""
Validation §11 sur données RÉELLES (LECTURE SEULE : rien n'est écrit, certifié ni recalculé ; ENOC et Snowflake ne sont ni lus en écriture ni modifiés).

    python manage.py fuel_validation_prod [--debut AAAA-MM-JJ --fin AAAA-MM-JJ] [--json chemin] [--nombre 5]

Produit : période exacte, bilans par statut, couverture target / CPH (par statut) / instrumentée / Closing, contrôles automatiques PASS/FAIL
(Ponctions = 0 L, aucun NULL transformé en 0, périodes identiques, CPH complète ≠ partielle, certifiée vide avant validation, target jamais dérivé
d'une puissance ou d'une courbe) et des sites réels pour chaque situation clé. Sans --debut/--fin : dernière période complète exploitable.
"""
import json
from collections import Counter
from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.management.commands.fuel_comparaison_controle import SITUATIONS
from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import day_bounds, default_period


def checks(rows, d1, d2) -> list[dict]:
    from django.db.models import Sum

    from fuel_tracking.models import FuelMovement, FuelSiteTarget

    out = []

    def add(code, ok, detail, n_bad=0):
        out.append({"controle": code, "resultat": "PASS" if ok else "FAIL", "detail": detail, "anomalies": n_bad})

    start, end = day_bounds(d1, d2)
    tr = FuelMovement.objects.filter(transfer_id__isnull=False, movement_datetime__gte=start, movement_datetime__lte=end).exclude(status="REJECTED")
    per_transfer = {t["transfer_id"]: float(t["s"] or 0) for t in tr.values("transfer_id").annotate(s=Sum("impact_stock_l"))}
    bad = {k: v for k, v in per_transfer.items() if abs(v) > 0.5}
    total = round(sum(per_transfer.values()), 3)
    add("PONCTIONS_SOMME_GLOBALE_ZERO", abs(total) <= 0.5 and not bad,
        f"{len(per_transfer)} transfert(s) ; somme globale {total} L ; {len(bad)} transfert(s) non équilibré(s)" + (f" : {list(bad.items())[:5]}" if bad else ""), len(bad))
    null_zero = [r["site_id"] for r in rows if (r["stock"]["initial_l"] is None or r["stock"]["final_l"] is None) and r["stock"]["variation_l"] is not None]
    add("AUCUN_NULL_TRANSFORME_EN_ZERO", not null_zero, f"{len(null_zero)} site(s) avec variation alors qu'un stock borne est absent" + (f" : {null_zero[:5]}" if null_zero else ""), len(null_zero))
    mismatch = [r["site_id"] for r in rows if r["instrumentee"]["ecart_l"] is not None and r["instrumentee"]["jours_mesure"] != r["instrumentee"]["jours"]]
    add("COMPARAISONS_PERIODES_IDENTIQUES", not mismatch, f"{len(mismatch)} écart(s) instrumentée calculé(s) sur une couverture partielle", len(mismatch))
    both = [r["site_id"] for r in rows if r["cph"]["complete_l"] is not None and r["cph"]["partielle_l"] is not None]
    add("CPH_COMPLETE_SEPAREE_DE_PARTIELLE", not both, f"{len(both)} site(s) avec les deux valeurs simultanément", len(both))
    cert = [r["site_id"] for r in rows if r["certification"]["valeur_l"] is not None and r["stock"]["statut"] != C.CERTIFIEE]
    add("CERTIFIEE_VIDE_AVANT_VALIDATION", not cert, f"{len(cert)} site(s) avec une valeur certifiée hors statut certifié", len(cert))
    neg_cert = [r["site_id"] for r in rows if r["stock"]["variation_l"] is not None and r["stock"]["variation_l"] < 0 and r["certification"]["valeur_l"] is not None]
    add("VARIATION_NEGATIVE_JAMAIS_CERTIFIEE", not neg_cert, f"{len(neg_cert)} variation(s) négative(s) certifiée(s)", len(neg_cert))
    sources = Counter(FuelSiteTarget.objects.values_list("source", flat=True)) if hasattr(FuelSiteTarget, "source") else Counter()
    odd = {k: v for k, v in sources.items() if k and "CLOSING" not in str(k).upper() and "TARGET" not in str(k).upper()}
    add("TARGET_JAMAIS_DERIVE_PUISSANCE_OU_COURBE", not odd, f"sources de target en base : {dict(sources) or 'aucune (aucun target importé)'}", len(odd))
    return out


def build_report(d1, d2, per_situation: int = 5) -> dict:
    res = C.build(d1, d2, {}, with_cph_quality=False)
    rows, k = res["rows"], res["kpis"]
    used = [r for r in rows if r["correspondance"]["utilise"]]
    n = len(used) or 1
    pct = lambda x: round(100.0 * x / n, 1)  # noqa: E731
    cph_by = Counter((r["stock"]["statut"], "complete" if r["cph"]["complete_l"] is not None else "partielle" if r["cph"]["partielle_l"] is not None else "aucune") for r in used)
    samples = {}
    for name, fn in SITUATIONS.items():
        hit = [r["site_id"] for r in rows if fn(r)]
        samples[name] = {"nombre": len(hit), "exemples": hit[:per_situation]}
    return {
        "periode": res["periode"], "fraicheur": res["fraicheur"], "regle": res["periode"].get("regle"),
        "sites_affiches": len(rows), "sites_utilises_dans_les_totaux": len(used), "sites_hors_closing": len(rows) - len(used),
        "bilans_par_statut": dict(Counter(r["stock"]["statut"] for r in used)),
        "couverture": {"target_pct": pct(sum(1 for r in used if r["target"]["periode_l"] is not None)),
                       "cph_complete_pct": pct(sum(1 for r in used if r["cph"]["complete_l"] is not None)),
                       "cph_partielle_pct": pct(sum(1 for r in used if r["cph"]["partielle_l"] is not None)),
                       "instrumentee_pct": pct(sum(1 for r in used if r["instrumentee"]["disponible"])),
                       "closing_pct": pct(sum(1 for r in used if r["closing"]["conso_l"] is not None))},
        "cph_par_statut_bilan": {f"{s} / {c}": v for (s, c), v in sorted(cph_by.items())},
        "kpis": {"variation_total_l": k["variation"]["total_l"], "closing_total_l": k["closing"]["total_l"], "instrumentee_total_l": k["instrumentee"]["total_l"],
                 "cph_complete_total_l": k["cph_complete"]["total_l"], "target_total_l": k["target"]["total_periode_l"], "sites_avec_target": k["target"]["sites_avec_target"],
                 "ecart_global_vs_target_pct": k["target"]["ecart_global_pct"], "ponctions": k["ponctions"], "alertes": k["alertes"]},
        "situations_reelles": samples,
        "controles": checks(rows, d1, d2),
        "limites": ["Cascade de médianes CPH (§8.3) : module livré, mais aucune mesure intra-journalière n'est stockée dans eGrid → niveaux de médiane non mesurables ici.",
                    "Règles de puissance GE v2 (§8.2) : module livré, non branché au moteur CPH en production."],
    }


class Command(BaseCommand):
    help = "Validation §11 sur données réelles (lecture seule) : tableau de statuts, couvertures et contrôles PASS/FAIL."

    def add_arguments(self, parser):
        parser.add_argument("--debut")
        parser.add_argument("--fin")
        parser.add_argument("--nombre", type=int, default=5)
        parser.add_argument("--json", default=None)

    def handle(self, *args, debut=None, fin=None, nombre=5, json=None, **opts):  # noqa: A002
        if debut and fin:
            d1, d2 = datetime.strptime(debut[:10], "%Y-%m-%d").date(), datetime.strptime(fin[:10], "%Y-%m-%d").date()
        else:
            p = default_period()
            if not p:
                raise CommandError("Aucun bilan de stock calculé : lancer d'abord le calcul (compute_stock_balances).")
            d1, d2 = p
        rep = build_report(d1, d2, nombre)
        w = self.stdout.write
        w(f"=== Validation §11 — période exacte {d1:%d/%m/%Y} 00:00:00 → {d2:%d/%m/%Y} 23:59:59 — règle {rep['regle']} ===")
        w(f"Sites affichés {rep['sites_affiches']} · utilisés dans les totaux {rep['sites_utilises_dans_les_totaux']} · hors Closing {rep['sites_hors_closing']}")
        w(f"Bilans par statut : {rep['bilans_par_statut']}")
        w(f"Couverture (en % des sites utilisés) : {rep['couverture']}")
        w(f"CPH par statut de bilan : {rep['cph_par_statut_bilan']}")
        w(f"KPI : {rep['kpis']}")
        w("--- Contrôles automatiques ---")
        for c in rep["controles"]:
            w(f"[{c['resultat']}] {c['controle']} — {c['detail']}")
        w("--- Situations réelles (exemples de sites) ---")
        for k, v in rep["situations_reelles"].items():
            w(f"{k:34s} {v['nombre']:5d}  {v['exemples']}")
        w("--- Limites ---")
        for x in rep["limites"]:
            w(f"- {x}")
        if json:
            import json as _json

            with open(json, "w", encoding="utf-8") as fh:
                _json.dump(rep, fh, ensure_ascii=False, indent=2, default=str)
            w(f"Rapport JSON écrit : {json}")
