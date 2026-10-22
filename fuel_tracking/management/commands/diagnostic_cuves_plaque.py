"""
Diagnostic LECTURE SEULE des cuves validées dont la plaque est incohérente avec la géométrie (> 5 %) :
leurs relevés en cm restent en « volume géométrique seulement » et ne sont jamais retenus.

Pour chaque cuve, teste des hypothèses d'erreur de saisie / d'import et indique celle qui rend la géométrie
cohérente avec la plaque (± 5 %). Aucune donnée n'est modifiée : la correction reste une décision humaine.

    python manage.py diagnostic_cuves_plaque
    python manage.py diagnostic_cuves_plaque --csv /mnt/Suivi_carburant_prod/cuves_plaque_incoherente.csv
"""
import csv as csvlib
from collections import Counter

from django.core.management.base import BaseCommand

from fuel_tracking.services import tank_geometry as G

TOL = 0.05


def _f(v):
    return float(v) if v is not None else None


def hypotheses(t) -> list[tuple[str, str, float, float, float]]:
    """(code, libellé, d1, d2, d3) — famille / cotes alternatives plausibles."""
    fam, d1, d2, d3 = t.shape_family, _f(t.dimension_1_cm), _f(t.dimension_2_cm), _f(t.dimension_3_cm)
    out = [("ACTUEL", "cotes et forme actuelles", fam, d1, d2, d3)]
    if fam == "h-circ":
        if d2:
            out.append(("DIAMETRE_EN_COTE_2", "diamètre = cote 2 (cote 1 erronée)", "h-circ", d2, None, d3))
            out.append(("SECTION_ELLIPTIQUE", "section elliptique (cote 1 × cote 2)", "h-elli", d1, d2, d3))
            out.append(("RECTANGULAIRE", "cuve rectangulaire (cote 1 × cote 2 × cote 3)", "rect", d1, d2, d3))
        out.append(("D_ET_L_INVERSES", "diamètre et longueur inversés", "h-circ", d3, None, d1))
        out.append(("VERTICALE", "cuve verticale (D = cote 1, H = cote 3)", "v-cyl", d1, d3, None))
    elif fam in ("h-elli", "h-oblo", "rect"):
        out.append(("LARGEUR_HAUTEUR_INVERSEES", "largeur et hauteur inversées", fam, d2, d1, d3))
        if fam != "rect":
            out.append(("RECTANGULAIRE", "cuve rectangulaire", "rect", d1, d2, d3))
        out.append(("CIRCULAIRE_D_COTE1", "section circulaire D = cote 1", "h-circ", d1, None, d3))
        if d2:
            out.append(("CIRCULAIRE_D_COTE2", "section circulaire D = cote 2", "h-circ", d2, None, d3))
    elif fam == "v-cyl":
        out.append(("D_ET_H_INVERSES", "diamètre et hauteur inversés", "v-cyl", d2, d1, None))
        out.append(("HORIZONTALE", "cuve horizontale (D = cote 1, L = cote 2)", "h-circ", d1, None, d2))
    return out


def full(fam, d1, d2, d3, t):
    try:
        return G.full_volume_l(fam, d1, d2, d3, t.bottom_profile or "plat", _f(t.bottom_z_cm), t.top_profile or "plat", _f(t.top_z_cm))
    except Exception:
        return None


class Command(BaseCommand):
    help = "Cuves validées à plaque incohérente : hypothèses d'erreur testées (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=None)

    def handle(self, *args, csv, **opts):
        from django.db.models import Count

        from fuel_tracking.models import FuelStockObservation as O, FuelTankMaster as T

        blocked = dict(O.objects.filter(conversion_status="GEOMETRIC_ONLY").values_list("site_id").annotate(n=Count("id")))
        tanks = [t for t in T.objects.filter(validation_status="VALIDEE", active=True) if t.site_id in blocked]
        rows, best_counter, ratio_counter, labels = [], Counter(), Counter(), {}
        for t in tanks:
            plate = _f(t.capacity_plate_l)
            res = []
            for code, label, fam, d1, d2, d3 in hypotheses(t):
                labels[code] = label
                v = full(fam, d1, d2, d3, t)
                gap = (v - plate) / plate if (v and plate) else None
                res.append((code, label, v, gap))
            cur = res[0]
            ok = [r for r in res[1:] if r[3] is not None and abs(r[3]) <= TOL]
            src_cap = _f(t.capacity_l)
            cap_ok = bool(src_cap and cur[2] and abs(src_cap - cur[2]) / cur[2] <= TOL)
            if ok:
                best = min(ok, key=lambda r: abs(r[3]))
                verdict = best[0]
            elif cap_ok:
                verdict = "PLAQUE_ERRONEE"  # la capacité déclarée par la source colle aux cotes : la plaque est fausse
            else:
                verdict = "A_RELEVER_SUR_SITE"
            best_counter[verdict] += 1
            if cur[2] and plate:
                r = cur[2] / plate
                ratio_counter["géométrie ≈ 3 à 4 × plaque" if 3 <= r <= 4 else "géométrie > 4 × plaque" if r > 4 else
                              "géométrie 1,05 à 3 × plaque" if r > 1.05 else "géométrie < plaque"] += 1
            rows.append({"site_id": t.site_id, "cuve": t.tank_id, "forme": t.shape_family, "cote1": _f(t.dimension_1_cm),
                         "cote2": _f(t.dimension_2_cm), "cote3": _f(t.dimension_3_cm), "plaque_l": plate, "capacite_source_l": src_cap,
                         "geometrie_actuelle_l": round(cur[2]) if cur[2] else None,
                         "ecart_actuel_pct": round(cur[3] * 100, 1) if cur[3] is not None else None,
                         "releves_bloques": blocked.get(t.site_id, 0), "verdict": verdict,
                         "hypotheses": " | ".join(f"{c}: {round(v) if v else '—'} L ({round(g * 100):+d} %)" for c, _, v, g in res[1:] if g is not None)})
        w = self.stdout.write
        w(f"Cuves validées à plaque incohérente (relevés bloqués) : {len(tanks)} — relevés bloqués : {sum(r['releves_bloques'] for r in rows)}")
        w("Rapport géométrie actuelle / plaque : " + ", ".join(f"{k} = {v}" for k, v in ratio_counter.most_common()))
        w("Hypothèse qui rend la cuve cohérente (± 5 %) :")
        labels.update({"PLAQUE_ERRONEE": "cotes cohérentes avec la capacité déclarée par la source : capacité de plaque probablement fausse",
                       "A_RELEVER_SUR_SITE": "aucune hypothèse ne colle : cotes et plaque à relever sur site"})
        for k, v in best_counter.most_common():
            w(f"  {k:<26} {v:>4}  {labels.get(k, '')}")
        w("\nExemples :")
        for r in sorted(rows, key=lambda r: -r["releves_bloques"])[:15]:
            w(f"  {r['site_id']} cuve {r['cuve']} {r['forme']} {r['cote1']}/{r['cote2']}/{r['cote3']} cm, plaque {r['plaque_l']} L, "
              f"géométrie {r['geometrie_actuelle_l']} L ({r['ecart_actuel_pct']} %) → {r['verdict']}")
        if csv:
            with open(csv, "w", newline="", encoding="utf-8") as f:
                wr = csvlib.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["site_id"], delimiter=";")
                wr.writeheader()
                wr.writerows(rows)
            w(f"\nListe complète : {csv}")
        w("\nAucune donnée modifiée. Corriger les cotes / la plaque dans la fiche cuve : les relevés sont reconvertis automatiquement.")
