"""
Plan de ravitaillement : quels sites livrer, avant quelle date, et combien.

  GET /api/fuel-tracking/bi/ravitaillement/?zone=&horizon=7&securite=2&cible=90
  GET /api/fuel-tracking/bi/export/ravitaillement/   (classeur Excel complet, mêmes paramètres)

Mêmes données et même autonomie que Suivi Stock (views_bi.stock_sites) : stock capteur au dernier relevé,
capacité de la cuve, conso mesurée moyenne par jour du mois de référence.

Règles (paramètres de l'écran, valeurs par défaut à valider avec le métier) :
  - stock estimé aujourd'hui = stock au relevé − conso/jour × jours écoulés depuis le relevé (jamais sous 0) ;
    c'est une ESTIMATION, affichée comme telle à côté du stock relevé et de sa date ;
  - jours avant rupture = stock estimé ÷ conso/jour ;
  - à livrer au plus tard = aujourd'hui + (jours avant rupture − marge de sécurité), jamais avant aujourd'hui ;
  - le site entre dans le plan si cette date tombe dans l'horizon ;
  - quantité = capacité × remplissage cible − stock estimé à la date de livraison, arrondie aux 50 L supérieurs ;
    capacité inconnue → quantité non calculable (jamais inventée) ;
  - URGENT : à livrer aujourd'hui ou demain.
Sites sans conso mesurée ou sans stock capteur : hors plan, comptés et listés avec leur motif.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import timedelta

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.views_bi import SRC_STOCK, _last_sync, _v, _x, stock_sites

DEFAULTS = {"horizon": 7, "securite": 2, "cible": 90}
LIMITS = {"horizon": (1, 30), "securite": (0, 10), "cible": (50, 100)}
RELEVE_ANCIEN_J = 7


class BadParam(ValueError):
    pass


def _int_param(params, key) -> int:
    raw = (params.get(key) or "").strip() if hasattr(params, "get") else ""
    if raw == "":
        return DEFAULTS[key]
    try:
        v = int(raw)
    except ValueError:
        raise BadParam(f"Paramètre « {key} » invalide : nombre entier attendu.")
    lo, hi = LIMITS[key]
    if not lo <= v <= hi:
        raise BadParam(f"Paramètre « {key} » hors limites ({lo} à {hi}).")
    return v


def _ceil50(v: float) -> int:
    return int(math.ceil(v / 50.0) * 50)


def compute_plan(params) -> dict:
    from fuel_tracking.models import FuelStockSyncRun

    horizon, securite, cible = (_int_param(params, k) for k in ("horizon", "securite", "cible"))
    base = stock_sites(params)
    today, sites, ref_month = base["today"], base["sites"], base["ref_month"]
    end = today + timedelta(days=horizon)

    plan, hors_plan = [], []
    nb_sans_conso = nb_sans_stock = nb_ok = 0
    for s in sites:
        if s["stock_l"] is None:
            nb_sans_stock += 1
            hors_plan.append({**_ident(s), "motif": "Pas de stock capteur : niveau inconnu"})
            continue
        if not s["conso_jour_l"]:
            nb_sans_conso += 1
            hors_plan.append({**_ident(s), "motif": f"Pas de conso mesurée en {ref_month or 'mois de référence'} : date de rupture non calculable"})
            continue
        d = float(s["conso_jour_l"])
        age = max(0, s["age_releve_j"] or 0)
        stock_now = max(0.0, float(s["stock_l"]) - d * age)
        jours_rupture = stock_now / d
        jours_livraison = max(0.0, jours_rupture - securite)
        livrer_avant = today + timedelta(days=math.floor(jours_livraison))
        if livrer_avant > end:
            nb_ok += 1
            continue
        stock_a_livraison = max(0.0, stock_now - d * math.floor(jours_livraison))
        cap = s["capacite_l"]
        quantite = _ceil50(cap * cible / 100.0 - stock_a_livraison) if cap else None
        if quantite is not None and quantite <= 0:
            quantite = None
        plan.append({
            **_ident(s),
            "stock_releve_l": s["stock_l"], "date_releve": s["date"], "age_releve_j": s["age_releve_j"],
            "releve_ancien": s["age_releve_j"] is None or s["age_releve_j"] > RELEVE_ANCIEN_J,
            "stock_estime_l": round(stock_now), "capacite_l": cap, "conso_jour_l": s["conso_jour_l"],
            "jours_avant_rupture": round(jours_rupture, 1),
            "date_rupture": today + timedelta(days=math.floor(jours_rupture)),
            "livrer_avant": livrer_avant,
            "priorite": "URGENT" if livrer_avant <= today + timedelta(days=1) else "PLANIFIE",
            "quantite_l": quantite,
            "quantite_motif": None if quantite is not None else ("Capacité de cuve inconnue" if not cap else "Cuve déjà au-dessus du remplissage cible"),
        })

    plan.sort(key=lambda r: (r["livrer_avant"], r["jours_avant_rupture"]))
    urgent = [r for r in plan if r["priorite"] == "URGENT"]
    litres = sum(r["quantite_l"] or 0 for r in plan)
    sans_qte = sum(1 for r in plan if r["quantite_l"] is None)

    by_zone = defaultdict(lambda: {"nb_sites": 0, "nb_urgent": 0, "quantite_l": 0})
    for r in plan:
        z = by_zone[r["zone"] or "Non renseignée"]
        z["nb_sites"] += 1
        z["nb_urgent"] += r["priorite"] == "URGENT"
        z["quantite_l"] += r["quantite_l"] or 0
    by_day = []
    for i in range(horizon + 1):
        day = today + timedelta(days=i)
        rows = [r for r in plan if r["livrer_avant"] == day]
        by_day.append({"date": day, "nb_sites": len(rows), "quantite_l": sum(r["quantite_l"] or 0 for r in rows)})

    maj = _last_sync(FuelStockSyncRun)
    regle = (f"Stock estimé aujourd'hui ÷ conso mesurée par jour ({ref_month or '—'}) = jours avant rupture ; "
             f"à livrer {securite} jour(s) avant la rupture ; quantité pour remplir la cuve à {cible} %.")
    per = {"zone": params.get("zone") or "Toutes", "horizon": f"{horizon} jours, du {today:%d/%m} au {end:%d/%m}"}
    excl = [{"label": "Sites sans stock capteur", "nb": nb_sans_stock},
            {"label": f"Sites sans conso mesurée en {ref_month or 'mois de référence'}", "nb": nb_sans_conso}]
    kpis = {
        "nb_sites": len(plan), "nb_urgent": len(urgent), "quantite_l": litres, "nb_sans_quantite": sans_qte,
        "nb_hors_plan": len(hors_plan), "nb_sites_ge": len(sites), "nb_au_dela": nb_ok,
        "nb_releves_anciens": sum(1 for r in plan if r["releve_ancien"]),
    }
    explain = {
        "nb_sites": _x("Sites à livrer", regle, [
            _v("Sites dans l'horizon", len(plan)), _v("Sites au-delà de l'horizon", nb_ok),
            _v("Horizon", horizon, "jours"), _v("Marge de sécurité", securite, "jours"),
        ], [SRC_STOCK, "Conso mesurée par capteur, moyenne par jour du mois de référence"], per, excl, maj,
            "Stock estimé : le stock relevé moins la conso des jours écoulés depuis le relevé. Un relevé de plus de "
            f"{RELEVE_ANCIEN_J} jours est signalé : à confirmer avant de livrer."),
        "nb_urgent": _x("À livrer aujourd'hui ou demain", "Sites dont la date « à livrer au plus tard » tombe aujourd'hui ou demain.", [
            _v("Sites urgents", len(urgent)), _v("Dont déjà sous la marge de sécurité", sum(1 for r in urgent if r["jours_avant_rupture"] <= securite)),
        ], [SRC_STOCK], per, excl, maj),
        "quantite_l": _x("Litres à livrer", f"Somme, site par site, de : capacité × {cible} % − stock estimé le jour de la livraison, arrondi aux 50 L supérieurs.", [
            _v("Total", litres, "L"), _v("Sites sans quantité calculable", sans_qte),
        ], [SRC_STOCK], per, excl + [{"label": "Sites du plan à la capacité de cuve inconnue (quantité non calculée)", "nb": sans_qte}], maj),
        "nb_hors_plan": _x("Sites hors plan", "Sites GE dont la date de rupture ne peut pas être calculée.", [
            _v("Sans stock capteur", nb_sans_stock), _v("Sans conso mesurée", nb_sans_conso),
        ], [SRC_STOCK], per, None, maj),
    }
    return {
        "parametres": {"horizon": horizon, "securite": securite, "cible": cible, "mois_reference": ref_month, "aujourdhui": today},
        "filters": {"zones": base["zones"]}, "kpis": kpis, "explain": explain,
        "plan": plan, "hors_plan": hors_plan,
        "by_zone": sorted([{"zone": k, **v} for k, v in by_zone.items()], key=lambda z: (-z["nb_urgent"], -z["nb_sites"])),
        "by_day": by_day,
    }


def _ident(s):
    return {"site_id": s["site_id"], "site_name": s["site_name"], "zone": s["zone"]}


class BiRavitaillementView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            return Response(compute_plan(request.query_params))
        except BadParam as e:
            return Response({"detail": str(e)}, status=400)


def export_plan(params):
    from openpyxl import Workbook

    from fuel_tracking.views_bi import _sheet, _summary_sheet, _xlsx_response

    d = compute_plan(params)
    k, p = d["kpis"], d["parametres"]
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Plan de ravitaillement", [
        ("Zone", params.get("zone") or "Toutes"), ("Horizon", f"{p['horizon']} jours"),
        ("Marge de sécurité", f"{p['securite']} jours"), ("Remplissage cible", f"{p['cible']} %"),
        ("Mois de référence de la conso", p["mois_reference"] or "—"),
    ], [
        ("Sites à livrer", k["nb_sites"], "int", "nb_sites"), ("Dont à livrer aujourd'hui ou demain", k["nb_urgent"], "int", "nb_urgent"),
        ("Litres à livrer", k["quantite_l"], "L", "quantite_l"), ("Sites sans quantité calculable", k["nb_sans_quantite"], "int"),
        ("Sites hors plan (rupture non calculable)", k["nb_hors_plan"], "int", "nb_hors_plan"),
    ], d["explain"])
    _sheet(wb, "Plan", [
        ("À livrer avant", "livrer_avant", "date", 13), ("Priorité", lambda r: "Urgent" if r["priorite"] == "URGENT" else "Planifié", "txt", 10),
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Zone", "zone", "txt", 10),
        ("Quantité à livrer (L)", "quantite_l", "L", 14), ("Motif si non calculée", "quantite_motif", "txt", 26),
        ("Rupture estimée", "date_rupture", "date", 13), ("Jours avant rupture", "jours_avant_rupture", "dec1", 11),
        ("Stock relevé (L)", "stock_releve_l", "L", 12), ("Date du relevé", "date_releve", "date", 12),
        ("Relevé ancien (> 7 j)", lambda r: "Oui" if r["releve_ancien"] else "", "txt", 11),
        ("Stock estimé aujourd'hui (L)", "stock_estime_l", "L", 14), ("Capacité (L)", "capacite_l", "L", 12),
        ("Conso / jour (L)", "conso_jour_l", "dec1", 11),
    ], d["plan"])
    _sheet(wb, "Par zone", [("Zone", "zone", "txt", 18), ("Sites", "nb_sites", "int", 9), ("Urgents", "nb_urgent", "int", 9),
                            ("Litres", "quantite_l", "L", 12)], d["by_zone"])
    _sheet(wb, "Hors plan", [("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Zone", "zone", "txt", 10),
                             ("Motif", "motif", "txt", 60)], d["hors_plan"])
    return _xlsx_response(wb, f"plan_ravitaillement_{timezone.localdate():%Y-%m-%d}.xlsx")
