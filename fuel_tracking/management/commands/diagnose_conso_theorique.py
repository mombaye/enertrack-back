# fuel_tracking/management/commands/diagnose_conso_theorique.py
"""
DIAGNOSTIC de la « Consommation théorique (L) » du Suivi Conso — LECTURE SEULE.

Pour chaque mois demandé, relit le tableau tel qu'affiché (mêmes fonctions que l'écran, ENOC et Snowflake jamais appelés :
cache et base EnerTrack seulement, aucune tâche de fond lancée) et compte, site par site, le maillon de la chaîne qui bloque :

  1. Puissance retenue   max(médiane GE, médiane redresseur), sinon Load DC telco + Recharge batterie
  2. Puissance PRP (kW)  GENSET DB du Closing + type de GE (un seul GE sur le site)
  3. Taux de charge      puissance retenue ÷ PRP ≤ 105 %
  4. Courbe CPH a, b, c  GENSET DB selon le type de GE
  5. Temps de marche     Index FMS → relevé de terrain → day_dg_on → day_rectifier_on (hors réseau)

  python manage.py diagnose_conso_theorique --mois 2026-09 --mois 2026-10 [--csv /tmp/conso_theorique.csv]
"""
from __future__ import annotations


import re
from collections import Counter
from datetime import date, timedelta
from unittest import mock

from django.core.management.base import BaseCommand

STEPS = ("1. Puissance retenue n.d.", "2. Puissance PRP n.d.", "2b. Plusieurs GE sur le site", "3. Taux de charge > 105 %",
         "4. Courbe CPH absente", "4b. CPH ≤ 0", "5. Temps de marche n.d.")


def blocking_step(e: dict) -> str | None:
    """Premier maillon bloquant de la chaîne, dans l'ordre du calcul ; None si la conso théorique est calculée."""
    if e.get("conso_theorique_l") is not None:
        return None
    if e.get("p_retenue_kw") is None:
        return STEPS[0]
    m = e.get("taux_charge_motif") or ""
    if "plusieurs GE" in m:
        return STEPS[2]
    if e.get("taux_charge") is None:
        return STEPS[3] if "105 %" in m else STEPS[1]
    if e.get("cph_l_h") is None:
        return STEPS[5] if "CPH ≤ 0" in (e.get("conso_theorique_motif") or "") else STEPS[4]
    return STEPS[6]


def _norm(m: str | None) -> str:
    """Motif sans les nombres ni les noms propres, pour regrouper les raisons identiques."""
    m = re.sub(r"« [^»]* »", "« … »", m or "(aucun motif)")
    return re.sub(r"\d+(?:[.,]\d+)?", "#", m)


class Command(BaseCommand):
    help = "Diagnostic lecture seule : pourquoi la Consommation théorique (L) est n.d., maillon par maillon."

    def add_arguments(self, parser):
        parser.add_argument("--mois", action="append", default=None, help="AAAA-MM, répétable (défaut : 2026-09 et 2026-10).")
        parser.add_argument("--csv", default=None, help="Détail par site (une ligne par site et par mois).")

    def handle(self, *args, mois, csv: str | None, **opts):
        from fuel_tracking.services import conso_comparison as C
        from fuel_tracking.services import stock_initial_sources as S

        w = self.stdout.write
        detail: list[dict] = []
        # Lecture seule stricte : aucune synchro Snowflake ni rafraîchissement ENOC déclenché par l'affichage
        with mock.patch.object(S, "request_fact_days", lambda days: None), \
                mock.patch("fuel_tracking.tasks.refresh_ge_info.delay", lambda *a, **k: None, create=True):
            for m in mois or ["2026-09", "2026-10"]:
                y, mo = map(int, m.split("-"))
                d_from, d_to = date(y, mo, 1), date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)
                rows = C.build(d_from, d_to, {}, with_cph_quality=False)["rows"]
                S.enrich(rows, fetch=False)
                self._report(w, m, rows, detail)
        if csv:
            with open(csv, "w", newline="", encoding="utf-8") as fh:
                wr = __import__("csv").DictWriter(fh, fieldnames=list(detail[0]) if detail else ["mois"], delimiter=";")
                wr.writeheader()
                wr.writerows(detail)
            w(f"\nDétail par site : {csv} ({len(detail)} lignes)")

    def _report(self, w, m, rows, detail):  # noqa: C901
        n = len(rows)
        es = [(r, r.get("estimation_theorique") or {}) for r in rows]
        ok = [e for _, e in es if e.get("conso_theorique_l") is not None]
        w(f"\n== {m} : {n} sites du tableau ==")
        w(f"Conso théorique calculée : {len(ok)} ({100 * len(ok) / n:.0f} %)" if n else "Aucun site")
        if not n:
            return
        w(f"  dont charge < 50 % (extrapolation)  : {sum(1 for e in ok if e.get('cph_extrapolation'))}")
        w(f"  dont 0 L (GE à l'arrêt)              : {sum(1 for e in ok if e.get('conso_theorique_l') == 0)}")

        w("\n-- Disponibilité de chaque maillon (tous sites) --")
        have = lambda k: sum(1 for _, e in es if e.get(k) is not None)  # noqa: E731
        for label, k in (("Puissance médiane générateur", "p_ge_kw"), ("Puissance médiane redresseur", "p_rect_kw"),
                         ("Load DC telco", "p_load_dc_kw"), ("Recharge batterie", "p_batt_kw"),
                         ("Puissance retenue", "p_retenue_kw"), ("Type de GE", "type_ge"), ("Taux de charge", "taux_charge"),
                         ("CPH (L/h)", "cph_l_h"), ("Temps de marche GE retenu", "tm_retenu_h")):
            w(f"  {label:<32} {have(k):>5} / {n}")
        prp1 = sum(1 for _, e in es if len(e.get("prp_kw") or []) == 1)
        prpn = sum(1 for _, e in es if len(e.get("prp_kw") or []) > 1)
        w(f"  {'Puissance PRP (1 GE)':<32} {prp1:>5} / {n}   (plusieurs GE : {prpn})")

        w("\n-- Source de la puissance retenue --")
        for k, v in Counter(e.get("p_retenue_source") or "n.d." for _, e in es).most_common():
            w(f"  {k:<34} {v:>5}")
        w("-- Source du temps de marche retenu --")
        for k, v in Counter(e.get("tm_source") or "n.d." for _, e in es).most_common():
            w(f"  {k:<34} {v:>5}")

        w("\n-- Premier maillon bloquant (sites sans conso théorique) --")
        steps = Counter(blocking_step(e) for _, e in es if e.get("conso_theorique_l") is None)
        for s in STEPS:
            if steps.get(s):
                w(f"  {s:<34} {steps[s]:>5}")

        w("\n-- Sites débloqués si UN SEUL maillon manquant était fourni --")
        only = Counter()
        for _, e in es:
            if e.get("conso_theorique_l") is not None:
                continue
            miss = [k for k, cond in (("puissance", e.get("p_retenue_kw") is None),
                                      ("PRP / type de GE", not e.get("prp_kw") or len(e.get("prp_kw") or []) > 1),
                                      ("courbe CPH", not e.get("courbes_cph") or not any(e.get("courbes_cph") or [])),
                                      ("temps de marche", e.get("tm_retenu_h") is None)) if cond]
            only[miss[0] if len(miss) == 1 else f"{len(miss)} maillons manquants" if miss else "autre (charge > 105 %, CPH ≤ 0)"] += 1
        for k, v in only.most_common():
            w(f"  {k:<34} {v:>5}")

        w("\n-- Motifs détaillés (regroupés) --")
        for label, key in (("Puissance retenue", "p_retenue_motif"), ("Puissance générateur", "p_ge_motif"),
                           ("Puissance redresseur", "p_rect_motif"), ("Load DC", "p_load_dc_motif"),
                           ("PRP", "prp_motif"), ("Conso théorique", "conso_theorique_motif")):
            c = Counter(_norm(e.get(key)) for _, e in es if e.get(key))
            if c:
                w(f"  {label} :")
                for k, v in c.most_common(6):
                    w(f"    {v:>5}  {k[:140]}")

        for r, e in es:
            detail.append({"mois": m, "site_id": r["site_id"], "type_ge": e.get("type_ge"), "prp_kw": e.get("prp_kw"),
                           "p_ge_kw": e.get("p_ge_kw"), "p_rect_kw": e.get("p_rect_kw"), "p_load_dc_kw": e.get("p_load_dc_kw"),
                           "p_batt_kw": e.get("p_batt_kw"), "p_retenue_kw": e.get("p_retenue_kw"),
                           "p_retenue_source": e.get("p_retenue_source"), "taux_charge": e.get("taux_charge"),
                           "cph_l_h": e.get("cph_l_h"), "tm_retenu_h": e.get("tm_retenu_h"), "tm_source": e.get("tm_source"),
                           "conso_theorique_l": e.get("conso_theorique_l"), "maillon_bloquant": blocking_step(e),
                           "motif": e.get("conso_theorique_motif") or e.get("p_retenue_motif")})
