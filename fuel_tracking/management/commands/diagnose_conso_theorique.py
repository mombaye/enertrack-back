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

Comparaison avec Ops : « Estimation Conso Fuel Théorique retenue » de la MÊME ligne du fichier Output App Fuel (déjà importée,
contexte des relevés de clôture). L'écart total est décomposé en litres par cause : site n.d. chez nous (par maillon
bloquant), 0 L chez nous, valeur plus basse / plus haute, site sans valeur Ops. Aucune valeur n'est modifiée.

  python manage.py diagnose_conso_theorique --mois 2026-09 --mois 2026-10 [--csv /tmp/conso_theorique.csv]
                                            [--xlsx /tmp/conso_theorique.xlsx]

--xlsx : classeur Excel — onglets « Plus bas qu'Ops » (valeur calculée inférieure à Ops, trié par écart), « Sans puissance »
(conso n.d. car puissance retenue n.d., avec une valeur Ops) — ces deux onglets seulement.
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


def ops_cause(e: dict, o: float | None) -> str | None:
    """Cause de l'écart avec Ops pour un site (None : pas de valeur Ops, ou valeurs égales)."""
    if o is None:
        return None
    v = e.get("conso_theorique_l")
    if v is None:
        return f"n.d. chez nous — {blocking_step(e)}"
    if v == 0 and o > 0:
        return "0 L chez nous (temps de marche 0), Ops > 0"
    if v < o:
        return "valeur plus basse que Ops"
    if v > o:
        return "valeur plus haute que Ops"
    return None


def _norm(m: str | None) -> str:
    """Motif sans les nombres ni les noms propres, pour regrouper les raisons identiques."""
    m = re.sub(r"« [^»]* »", "« … »", m or "(aucun motif)")
    return re.sub(r"\d+(?:[.,]\d+)?", "#", m)


class Command(BaseCommand):
    help = "Diagnostic lecture seule : pourquoi la Consommation théorique (L) est n.d., maillon par maillon."

    def add_arguments(self, parser):
        parser.add_argument("--mois", action="append", default=None, help="AAAA-MM, répétable (défaut : 2026-09 et 2026-10).")
        parser.add_argument("--csv", default=None, help="Détail par site (une ligne par site et par mois).")
        parser.add_argument("--xlsx", default=None, help="Classeur Excel : sites plus bas qu'Ops et sites sans puissance.")

    def handle(self, *args, mois, csv: str | None, xlsx: str | None = None, **opts):
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
                ops = ops_values(rows)
                self._report(w, m, rows, detail, ops)
        if csv:
            with open(csv, "w", newline="", encoding="utf-8") as fh:
                wr = __import__("csv").DictWriter(fh, fieldnames=list(detail[0]) if detail else ["mois"], delimiter=";")
                wr.writeheader()
                wr.writerows(detail)
            w(f"\nDétail par site : {csv} ({len(detail)} lignes)")
        if xlsx:
            n_bas, n_pw = write_xlsx(xlsx, detail)
            w(f"\nClasseur Excel : {xlsx} — {n_bas} site(s) plus bas qu'Ops, {n_pw} site(s) sans puissance")

    def _report(self, w, m, rows, detail, ops):  # noqa: C901
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

        self._compare_ops(w, es, ops)

        for r, e in es:
            o = ops.get(r["site_id"]) or {}
            v, ol = e.get("conso_theorique_l"), o.get("l")
            detail.append({"ops_conso_theorique_l": ol, "ops_source": o.get("source"),
                           "ecart_ops_l": round(ol - (v or 0), 2) if ol is not None else None, "cause_ecart_ops": ops_cause(e, ol),
                           "site_name": r.get("site_name"), "fabricant_ge": e.get("fabricant_ge"),
                           "prp_source": e.get("prp_source"), "cph_extrapolation": e.get("cph_extrapolation"),
                           "p_ge_motif": e.get("p_ge_motif"), "p_rect_motif": e.get("p_rect_motif"),
                           "p_load_dc_motif": e.get("p_load_dc_motif"), "p_retenue_motif": e.get("p_retenue_motif"),
                           **{"mois": m, "site_id": r["site_id"], "type_ge": e.get("type_ge"), "prp_kw": e.get("prp_kw"),
                           "p_ge_kw": e.get("p_ge_kw"), "p_rect_kw": e.get("p_rect_kw"), "p_load_dc_kw": e.get("p_load_dc_kw"),
                           "p_batt_kw": e.get("p_batt_kw"), "p_retenue_kw": e.get("p_retenue_kw"),
                           "p_retenue_source": e.get("p_retenue_source"), "taux_charge": e.get("taux_charge"),
                           "cph_l_h": e.get("cph_l_h"), "tm_retenu_h": e.get("tm_retenu_h"), "tm_source": e.get("tm_source"),
                           "conso_theorique_l": e.get("conso_theorique_l"), "maillon_bloquant": blocking_step(e),
                           "motif": e.get("conso_theorique_motif") or e.get("p_retenue_motif")}})

    def _compare_ops(self, w, es, ops):
        """Écart en litres entre notre conso théorique et celle d'Ops (même ligne de fichier), décomposé par cause."""
        if not ops:
            w("\n-- Comparaison avec Ops : aucune valeur Ops importée pour ce mois --")
            return
        ours_tot = sum(e.get("conso_theorique_l") or 0 for _, e in es)
        both = [(r, e, ops[r["site_id"]]["l"]) for r, e in es if (ops.get(r["site_id"]) or {}).get("l") is not None]
        ops_tot = sum(o for _, _, o in both)
        w(f"\n-- Comparaison avec Ops (Estimation Conso Fuel Théorique retenue, même ligne du fichier) --")
        w(f"  Total EnerTrack : {ours_tot:>10,.0f} L ({sum(1 for _, e in es if e.get('conso_theorique_l') is not None)} sites)")
        w(f"  Total Ops       : {ops_tot:>10,.0f} L ({len(both)} sites avec une valeur Ops, {sum(1 for *_, o in both if o > 0)} > 0)")
        causes = Counter()
        n = Counter()
        for r, e, o in both:
            v = e.get("conso_theorique_l")
            k = ops_cause(e, o)
            if k is None:
                continue
            causes[k] += o - (v or 0)
            n[k] += 1
        extra = sum(e.get("conso_theorique_l") or 0 for r, e in es if (ops.get(r["site_id"]) or {}).get("l") is None)
        w("  Écart Ops − EnerTrack par cause (L, sites) :")
        for k, v in sorted(causes.items(), key=lambda x: -abs(x[1])):
            w(f"    {v:>10,.0f} L  {n[k]:>4} site(s)  {k}")
        if extra:
            w(f"    {-extra:>10,.0f} L        sites calculés chez nous sans valeur Ops")
        lower = [(r, e, o) for r, e, o in both if e.get("conso_theorique_l") not in (None, 0) and e["conso_theorique_l"] < o]
        if lower:
            ratio = sorted(e["conso_theorique_l"] / o for _, e, o in lower if o > 0)
            w(f"  Sites plus bas que Ops : ratio médian EnerTrack ÷ Ops = {ratio[len(ratio) // 2]:.2f} ; "
              f"dont charge < 50 % (extrapolation) : {sum(1 for _, e, _ in lower if e.get('cph_extrapolation'))}")


def ops_values(rows) -> dict:
    """site → {"l", "source"} : Estimation Conso Fuel Théorique retenue d'Ops sur la ligne du fichier du site (celle du tableau)."""
    from fuel_tracking.services import stock_initial_sources as S

    out = {}
    for sid, line in S._file_line_values(rows).items():
        c = line.get("fin") or {}
        if c.get("conso_theorique_ops_l") is not None:
            out[sid] = {"l": float(c["conso_theorique_ops_l"]), "source": c.get("source_conso_theorique_ops")}
    return out


# ── Classeur Excel (--xlsx) ──
XLSX_BAS = (("Mois", "mois"), ("Site ID", "site_id"), ("Nom du site", "site_name"), ("Fabricant GE", "fabricant_ge"),
            ("Type de GE", "type_ge"), ("PRP (kW)", "prp_kw"), ("Source PRP", "prp_source"), ("Puissance retenue (kW)", "p_retenue_kw"),
            ("Source puissance", "p_retenue_source"), ("Taux de charge", "taux_charge"), ("Charge < 50 % (extrapolation)", "cph_extrapolation"),
            ("CPH (L/h)", "cph_l_h"), ("Temps de marche (h)", "tm_retenu_h"), ("Source temps de marche", "tm_source"),
            ("Conso théorique EnerTrack (L)", "conso_theorique_l"), ("Conso théorique Ops (L)", "ops_conso_theorique_l"),
            ("Écart Ops − EnerTrack (L)", "ecart_ops_l"), ("Ratio EnerTrack ÷ Ops", "_ratio"))
XLSX_PW = (("Mois", "mois"), ("Site ID", "site_id"), ("Nom du site", "site_name"), ("Type de GE", "type_ge"),
           ("Conso théorique Ops (L)", "ops_conso_theorique_l"), ("Puissance médiane GE (kW)", "p_ge_kw"), ("Motif GE", "p_ge_motif"),
           ("Puissance médiane redresseur (kW)", "p_rect_kw"), ("Motif redresseur", "p_rect_motif"),
           ("Load DC telco (kW)", "p_load_dc_kw"), ("Motif Load DC", "p_load_dc_motif"), ("Recharge batterie (kW)", "p_batt_kw"),
           ("Motif puissance retenue", "p_retenue_motif"), ("Temps de marche (h)", "tm_retenu_h"))


def _cell(v):
    """Valeur Excel : nombre conservé comme nombre ; liste (plusieurs GE) jointe par « / » ; vide = n.d. (jamais 0)."""
    if isinstance(v, (list, tuple)):
        return " / ".join(f"{x:g}" if isinstance(x, (int, float)) else str(x) for x in v) if v else None
    if isinstance(v, bool):
        return "oui" if v else "non"
    try:
        from decimal import Decimal

        if isinstance(v, Decimal):
            return float(v)
    except Exception:  # noqa: BLE001
        pass
    return v


def write_xlsx(path: str, detail: list[dict]) -> tuple[int, int]:
    """Classeur : « Plus bas qu'Ops » (trié par écart décroissant) et « Sans puissance ». → (nb plus bas, nb sans puissance)."""
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    bas = sorted((d for d in detail if d.get("cause_ecart_ops") == "valeur plus basse que Ops"),
                 key=lambda d: -(d.get("ecart_ops_l") or 0))
    for d in bas:
        o, v = d.get("ops_conso_theorique_l"), d.get("conso_theorique_l")
        d["_ratio"] = round(float(v) / o, 3) if o and v is not None else None
    pw = sorted((d for d in detail if d.get("cause_ecart_ops") == f"n.d. chez nous — {STEPS[0]}"),
                key=lambda d: -(d.get("ops_conso_theorique_l") or 0))
    wb = openpyxl.Workbook()
    head_fill, head_font = PatternFill("solid", fgColor="1F3864"), Font(bold=True, color="FFFFFF")

    def sheet(ws, cols, items, total_keys=()):
        ws.append([h for h, _ in cols])
        for d in items:
            ws.append([_cell(d.get(k)) for _, k in cols])
        if items and total_keys:
            ws.append([("Total" if i == 0 else (round(sum(float(_cell(d.get(k)) or 0) for d in items), 2) if k in total_keys else None))
                       for i, (_, k) in enumerate(cols)])
            for c in ws[ws.max_row]:
                c.font = Font(bold=True)
        for i, c in enumerate(ws[1], start=1):
            c.fill, c.font = head_fill, head_font
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[get_column_letter(i)].width = 14 if i > 3 else 16
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = ws.dimensions

    ws = wb.active
    ws.title = "Plus bas qu'Ops"
    sheet(ws, XLSX_BAS, bas, {"conso_theorique_l", "ops_conso_theorique_l", "ecart_ops_l"})
    sheet(wb.create_sheet("Sans puissance"), XLSX_PW, pw, {"ops_conso_theorique_l"})
    wb.save(path)
    return len(bas), len(pw)
