"""
Tableau de validation terrain : ~10 sites RÉELS d'une période déjà calculée, choisis pour couvrir les situations clés
(stock direct en litres, cm + cuve validée, cuve géométrique sans plaque, cuve proposée, non calculable, Truck, Carte Total,
Ponction, mouvement à contrôler). LECTURE SEULE : aucune certification, aucune écriture (sauf avec --calculer, qui recalcule le bilan).

    python manage.py fuel_validation_sites --debut 2026-09-04 --fin 2026-10-04
    python manage.py fuel_validation_sites --debut 2026-09-04 --fin 2026-10-04 --nombre 10 --csv /tmp/validation_10_sites.csv --anonymiser
"""
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

COLUMNS = ["site_id", "periode", "source_stock_initial", "stock_initial_l", "source_stock_final", "stock_final_l", "truck_l", "carte_total_l",
           "transferts_entrants_l", "transferts_sortants_l", "conso_reelle_estimee_l", "statut", "cause", "score", "reserves", "motifs", "action_attendue", "profil_de_test"]

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
    "ENTREES_NON_RETROUVEES": "Compléter dans ENOC la/les livraison(s) du Closing absente(s) (Fuel Manager).",
    "MOUVEMENTS_NON_CONFIRMES_CLOSING": "Arbitrer ENOC vs Closing : entrée ENOC non confirmée, ou sortie du Closing absente d'eGrid.",
}


def action_for(b) -> str:
    if b.cause_principale in ACTIONS:
        return ACTIONS[b.cause_principale]
    if b.statut_bilan == "PRET_A_CERTIFIER":
        return "Certification manuelle par le Fuel Manager après contrôle des données."
    return "Compléter les données du site (cause non référencée : voir les motifs)."


def _mode(detail) -> str | None:
    return (detail or {}).get("mode_valeur")


def profils(b) -> list[str]:
    out = []
    mi, mf = _mode(b.stock_initial_detail), _mode(b.stock_final_detail)
    modes = {m for m in (mi, mf) if m}
    if b.statut_bilan == "NON_CALCULABLE":
        out.append("non_calculable")
    if modes and modes <= {"LITRE_DIRECT"}:
        out.append("stock_litre_direct")
    if modes and modes <= {"LITRE_DIRECT", "CM_CONVERTI_CUVE_VALIDEE"} and "CM_CONVERTI_CUVE_VALIDEE" in modes:
        out.append("cm_cuve_validee")
    if any(m and m.endswith("GEOMETRIQUE_RESERVE") for m in modes):
        out.append("cuve_geometrique_sans_plaque")
    if any(m and m.endswith("PROPOSEE_RESERVE") for m in modes):
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
    wanted = ["stock_litre_direct", "cm_cuve_validee", "cuve_geometrique_sans_plaque", "cuve_proposee", "non_calculable",
              "livraison_truck", "carte_total", "ponction", "mouvement_a_controler"]
    cand = [(b, set(profils(b))) for b in rows]
    covered, chosen = set(), []
    while cand and len(chosen) < n:
        cand.sort(key=lambda x: (-len((x[1] & set(wanted)) - covered), -(x[0].score_confiance or 0), x[0].site_id))
        b, p = cand.pop(0)
        chosen.append((b, sorted(p)))
        covered |= p
    return chosen


class Command(BaseCommand):
    help = "Tableau de validation sur ~10 sites réels d'une période déjà calculée (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--debut", required=True)
        parser.add_argument("--fin", required=True)
        parser.add_argument("--nombre", type=int, default=10)
        parser.add_argument("--csv", default=None)
        parser.add_argument("--anonymiser", action="store_true", help="Remplace les site_id par SITE_001, SITE_002…")
        parser.add_argument("--calculer", action="store_true", help="(Re)calcule d'abord le bilan de la période (aucune certification).")

    def handle(self, *args, debut, fin, nombre, csv: str | None = None, anonymiser, calculer, **opts):  # noqa: A002
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services import stock_period_balance as SB

        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(datetime.strptime(debut, "%Y-%m-%d").date(), time.min), tz)
        end = timezone.make_aware(datetime.combine(datetime.strptime(fin, "%Y-%m-%d").date(), time(23, 59, 59)), tz)
        if calculer:
            SB.compute_balances(start, end)
        rows = list(B.objects.filter(observation_start=start, observation_end=end))
        if not rows:
            raise CommandError("Aucun bilan pour cette période exacte : lancer compute_stock_balances (ou --calculer) avec les mêmes dates.")
        chosen = pick(rows, nombre)
        table = []
        for i, (b, p) in enumerate(chosen, start=1):
            f = lambda v: None if v is None else float(v)  # noqa: E731
            table.append({
                "site_id": f"SITE_{i:03d}" if anonymiser else b.site_id, "periode": f"{start:%d/%m/%Y} → {end:%d/%m/%Y}",
                "source_stock_initial": b.source_stock_initial, "stock_initial_l": f(b.stock_initial_l),
                "source_stock_final": b.source_stock_final, "stock_final_l": f(b.stock_final_l),
                "truck_l": f(b.livraisons_externes_l), "carte_total_l": f(b.carte_total_l),
                "transferts_entrants_l": f(b.transferts_entrants_l), "transferts_sortants_l": f(b.transferts_sortants_l),
                "conso_reelle_estimee_l": f(b.consommation_reelle_l), "statut": b.statut_bilan, "score": b.score_confiance,
                "reserves": " | ".join((b.reserves or [])[:3]) or "aucune",
                "motifs": " | ".join((b.motifs or [])[:2]),
                "cause": b.cause_principale, "action_attendue": action_for(b),
                "profil_de_test": ", ".join(p),
            })
        w = self.stdout.write
        w(f"Période {start:%d/%m/%Y} → {end:%d/%m/%Y} — {len(rows)} bilans, {len(table)} sites retenus (lecture seule, rien n'est certifié)\n")
        for r in table:
            w(" | ".join(f"{k}={'—' if v is None else v}" for k, v in r.items()))
        covered = {x for _, p in chosen for x in p}
        manquants = [x for x in ("stock_litre_direct", "cm_cuve_validee", "cuve_geometrique_sans_plaque", "cuve_proposee", "non_calculable",
                                 "livraison_truck", "carte_total", "ponction", "mouvement_a_controler") if x not in covered]
        w(f"\nProfils couverts : {sorted(covered)}")
        w(f"Profils non trouvés sur cette période : {manquants or 'aucun'}")
        if csv:
            with open(csv, "w", newline="", encoding="utf-8-sig") as fh:
                wr = __import__("csv").DictWriter(fh, fieldnames=COLUMNS, delimiter=";")
                wr.writeheader()
                wr.writerows(table)
            w(f"CSV : {csv}")
