"""
Tableau des données par site (un mois) : activité GE, conso mesurée ET conso estimée (jamais additionnées),
comparaisons, stock retenu, livraisons, alertes. Pagination serveur, export Excel complet.

  GET /api/fuel-tracking/bi/sites-table/?month=YYYY-MM&zone=&q=&alerte=&sort=&desc=1&page=1&page_size=50
  GET /api/fuel-tracking/bi/export/sites/   (mêmes filtres, toutes les lignes)

Sources :
  - conso mesurée, jours mesurés, kWh GE, livraisons ENOC : FuelConsommationMonthly (Snowflake VW_FUEL_REPORT, ENOC) ;
  - running time, CPH, charge, conso estimée (CPH × running time) : moteur CPH (cph_service.compute_period_summary) ;
  - target : targets actifs du Fuel Manager ;
  - stock : relevé le plus récent toutes sources (services/current_stock, via views_bi.stock_sites).
Absent ≠ 0 : une valeur inconnue reste null ; les totaux indiquent sur combien de sites ils portent.
"""
from __future__ import annotations

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.views_bi import (DEBUT_SUIVI, SCOPE_LABEL, SRC_CAPTEUR, SRC_STOCK, SRC_TARGET, _BadPeriod, _days_in, _f, _last_sync,
                                    _month_bounds, _site_zones, _v, _x, scope_q, stock_sites)

log = logging.getLogger(__name__)

SRC_CPH = "Moteur CPH : running time GE (gFMS) × consommation horaire du GE (abaque constructeur selon la charge)"
SRC_ENOC_LIV = "ENOC : litres ajoutés lors des ravitaillements du mois"
ECART_SEUIL_PCT = 20
SOUS_CHARGE_PCT = 30
L_KWH_MAX = 0.30
MIN_KWH = 100
MIN_PREV_L = 100
ALERTES = {
    "ECART_CPH": f"Écart mesuré / estimé > {ECART_SEUIL_PCT} %",
    "L_KWH_HAUT": f"Rendement > {L_KWH_MAX:.2f} L/kWh".replace(".", ","),
    "AU_DESSUS_TARGET": "Au-dessus du target",
    "SOUS_CHARGE": f"GE sous-chargé (< {SOUS_CHARGE_PCT} %)",
    "AUTONOMIE_3J": "Autonomie < 3 jours",
    "RELEVE_ANCIEN": "Relevé de stock > 7 jours",
}
# Alertes propres à chaque partie de l'écran : Suivi Conso et Suivi Stock affichent chacun les leurs.
VUES = {"conso": ("ECART_CPH", "L_KWH_HAUT", "AU_DESSUS_TARGET", "SOUS_CHARGE"), "stock": ("AUTONOMIE_3J", "RELEVE_ANCIEN")}
SORTS = ("site_id", "zone", "runtime_h", "cph_l_h", "charge_pct", "conso_mesuree_l", "conso_estimee_l", "ecart_pct", "ecart_l",
         "l_jour", "l_kwh", "evolution_pct", "ecart_target_pct", "stock_l", "autonomie_j", "enoc_livre_l", "nb_alertes")


def _ref_month(params) -> str | None:
    from fuel_tracking.models import FuelConsommationMonthly

    m = (params.get("month") or "").strip()
    if m:
        if len(m) != 7 or m[4] != "-" or not (m[:4] + m[5:]).isdigit() or not 1 <= int(m[5:]) <= 12:
            raise _BadPeriod("Mois invalide : format AAAA-MM attendu.")
        return m
    months = list(FuelConsommationMonthly.objects.order_by("-month_year").values_list("month_year", flat=True).distinct()[:2])
    return months[1] if len(months) > 1 else (months[0] if months else None)


def _prev_month(m: str) -> str:
    y, mo = int(m[:4]), int(m[5:])
    return f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"


def build_rows(month: str) -> tuple[list[dict], dict]:
    from fuel_tracking.models import FuelConsommationMonthly
    from fuel_tracking.services.fuel_target import active_targets, tolerance_pct

    start, end = _month_bounds(month, month)
    nb_days = max(1, (end - start).days + 1)
    zones = _site_zones()
    mrows = {r["site_id"]: r for r in FuelConsommationMonthly.objects.filter(scope_q(), month_year=month, has_genset=True).values(
        "site_id", "site_name", "typology", "typology_fichier", "configuration_fichier", "site_type_fichier", "pge_kva_fichier",
        "conso_snowflake_l", "nb_jours_data", "ge_prod_kwh", "enoc_qte_ajoutee_l", "enoc_nb_demandes")}
    # Pas de comparaison avec un mois antérieur au début du suivi (septembre 2026 n'a pas de « mois précédent »).
    prev = {} if _prev_month(month) < DEBUT_SUIVI else {
        r["site_id"]: r for r in FuelConsommationMonthly.objects.filter(month_year=_prev_month(month), site_id__in=list(mrows))
        .values("site_id", "conso_snowflake_l", "nb_jours_data")}

    cph, cph_ok = {}, True
    try:
        from fuel_tracking.services.cph_service import compute_period_summary
        cph = {r["site_id"]: r for r in compute_period_summary(start, end) if r.get("perimetre") == "GE"}
    except Exception:  # moteur CPH indisponible : colonnes CPH vides, signalé, le reste du tableau s'affiche
        log.exception("Tableau par site : moteur CPH indisponible")
        cph_ok = False
    stock = {s["site_id"]: s for s in stock_sites({"month": month})["sites"]}
    targets, tol = active_targets(), tolerance_pct()

    rows = []
    for sid in sorted(set(mrows) | {k for k, r in cph.items() if k not in mrows and _in_scope_cph(r)}):
        m, c, st, pv = mrows.get(sid) or {}, cph.get(sid) or {}, stock.get(sid) or {}, prev.get(sid) or {}
        meas = _f(m.get("conso_snowflake_l")) if m.get("conso_snowflake_l") is not None and float(m["conso_snowflake_l"]) > 0 else None
        jours = m.get("nb_jours_data") or 0
        l_jour = round(meas / jours, 1) if meas is not None and jours else None
        p_meas = float(pv["conso_snowflake_l"]) if pv.get("conso_snowflake_l") else None
        # Mois précédent sous 100 L : base trop faible, un pourcentage serait trompeur (ex. +29 900 %).
        p_ljour = p_meas / pv["nb_jours_data"] if p_meas and p_meas >= MIN_PREV_L and pv.get("nb_jours_data") else None
        theo = _f(c.get("conso_theorique_l"))
        rt = _f(c.get("runtime_total_h"))
        kwh = _f(m.get("ge_prod_kwh"))
        t = targets.get(sid.upper())
        tgt = _f(t.target_mensuel_l) if t is not None else None
        ecart_t = round((meas - tgt) / tgt * 100, 1) if meas is not None and tgt else None
        charge = _f(float(c["charge_moy"]) * 100) if c.get("charge_moy") is not None else None
        r = {
            "site_id": sid, "site_name": m.get("site_name") or c.get("site_name"), "zone": zones.get(sid) or c.get("zone"),
            "typologie": m.get("typology_fichier") or m.get("typology"), "configuration": m.get("configuration_fichier") or c.get("kind"),
            "energie": m.get("site_type_fichier"), "ge_kva": _f(m.get("pge_kva_fichier")) or _f(c.get("ge_kva")),
            "runtime_h": rt, "h_jour": round(rt / nb_days, 1) if rt is not None else None,
            "cph_l_h": _f(c.get("cph_moy_l_h"), 2), "charge_pct": charge, "statut_cph": c.get("cph_status") if c else None,
            "motif_cph": ((c.get("motif_cph") or {}).get("label") or (c.get("motif_cph") or {}).get("code")) if c else None,
            "ge_prod_kwh": kwh,
            "conso_mesuree_l": meas, "jours_mesures": jours or None, "l_jour": l_jour,
            "conso_estimee_l": theo,
            "ecart_l": _f(meas - theo) if meas is not None and theo is not None else None,
            "ecart_pct": round((meas - theo) / theo * 100, 1) if meas is not None and theo else None,
            "l_kwh": round(meas / kwh, 3) if meas is not None and kwh and kwh >= MIN_KWH else None,
            "conso_prec_l": _f(p_meas), "evolution_pct": round((l_jour - p_ljour) / p_ljour * 100, 1) if l_jour is not None and p_ljour else None,
            "target_l": tgt, "ecart_target_pct": ecart_t,
            "stock_l": st.get("stock_l"), "date_stock": st.get("date"), "source_stock": st.get("source_stock_label"),
            "stock_estime": bool(st.get("stock_estime")), "capacite_l": st.get("capacite_l"), "remplissage_pct": st.get("remplissage_pct"),
            "autonomie_j": st.get("autonomie_j"), "age_releve_j": st.get("age_releve_j"),
            "enoc_livre_l": _f(m.get("enoc_qte_ajoutee_l")) if m.get("enoc_nb_demandes") else None,
            "nb_livraisons": m.get("enoc_nb_demandes") or 0,
        }
        al = []
        if r["ecart_pct"] is not None and abs(r["ecart_pct"]) > ECART_SEUIL_PCT:
            al.append("ECART_CPH")
        if r["l_kwh"] is not None and r["l_kwh"] > L_KWH_MAX:
            al.append("L_KWH_HAUT")
        if ecart_t is not None and ecart_t > tol:
            al.append("AU_DESSUS_TARGET")
        if charge is not None and charge < SOUS_CHARGE_PCT and (rt or 0) > 0:
            al.append("SOUS_CHARGE")
        if r["autonomie_j"] is not None and r["autonomie_j"] < 3:
            al.append("AUTONOMIE_3J")
        if r["age_releve_j"] is not None and r["age_releve_j"] > 7:
            al.append("RELEVE_ANCIEN")
        r["alertes"], r["nb_alertes"] = al, len(al)
        rows.append(r)
    return rows, {"cph_disponible": cph_ok, "nb_jours": nb_days, "start": start, "end": end, "tolerance_target_pct": tol,
                  "zones": sorted({r["zone"] for r in rows if r["zone"]})}


def _in_scope_cph(r) -> bool:
    from fuel_tracking.views_bi import in_scope
    return in_scope(r.get("country"))


def _filter(rows, params):
    zone, q, alerte = params.get("zone") or "", (params.get("q") or "").strip().lower(), params.get("alerte") or ""
    if zone:
        rows = [r for r in rows if r["zone"] == zone]
    if q:
        rows = [r for r in rows if q in r["site_id"].lower() or q in (r["site_name"] or "").lower()]
    if alerte:
        rows = [r for r in rows if alerte in r["alertes"]]
    return rows


def _kpis(rows, meta, month, alertes=ALERTES):
    from fuel_tracking.models import FuelConsommationSyncRun, FuelDailyFactsSyncRun

    meas = [r for r in rows if r["conso_mesuree_l"] is not None]
    theo = [r for r in rows if r["conso_estimee_l"] is not None]
    both = [r for r in theo if r["conso_mesuree_l"] is not None]
    rt = [r for r in rows if r["runtime_h"] is not None]
    cph_both = [r for r in theo if r["runtime_h"]]
    s_meas, s_theo = sum(r["conso_mesuree_l"] for r in meas), sum(r["conso_estimee_l"] for r in theo)
    b_meas, b_theo = sum(r["conso_mesuree_l"] for r in both), sum(r["conso_estimee_l"] for r in both)
    rt_sum = sum(r["runtime_h"] for r in rt)
    cph_rt = sum(r["runtime_h"] for r in cph_both)
    n = len(rows)
    k = {
        "nb_sites": n,
        "conso_mesuree_l": _f(s_meas) if meas else None, "nb_sites_mesure": len(meas),
        "conso_estimee_l": _f(s_theo) if theo else None, "nb_sites_estime": len(theo),
        "ecart_l": _f(b_meas - b_theo) if both else None, "ecart_pct": round((b_meas - b_theo) / b_theo * 100, 1) if both and b_theo else None,
        "nb_sites_comparables": len(both), "conso_mesuree_comparable_l": _f(b_meas) if both else None, "conso_estimee_comparable_l": _f(b_theo) if both else None,
        "runtime_total_h": _f(rt_sum) if rt else None, "nb_sites_runtime": len(rt),
        "h_site_jour": round(rt_sum / (len(rt) * meta["nb_jours"]), 1) if rt else None,
        "cph_moyen_l_h": round(sum(r["conso_estimee_l"] for r in cph_both) / cph_rt, 2) if cph_rt else None,
        "nb_ecart_cph": sum(1 for r in rows if "ECART_CPH" in r["alertes"]),
        "nb_sites_alerte": sum(1 for r in rows if r["alertes"]),
    }
    per = [f"{SCOPE_LABEL}, sites avec groupe électrogène", f"Mois : {month} ({meta['start']:%d/%m} → {meta['end']:%d/%m}, {meta['nb_jours']} jours)"]
    maj_c, maj_cph = _last_sync(FuelConsommationSyncRun), _last_sync(FuelDailyFactsSyncRun)
    cph_note = None if meta["cph_disponible"] else "Moteur CPH indisponible au moment du calcul : conso estimée et running time absents."
    x = {
        "conso_mesuree_l": _x("Conso mesurée", "Somme des consommations mesurées par capteur du mois, site par site.", [
            _v("Total", k["conso_mesuree_l"], "L"), _v("Sites mesurés", len(meas)), _v("Sites GE affichés", n)], [SRC_CAPTEUR], per,
            [{"label": "Sites GE sans mesure capteur : non comptés (pas « 0 L »)", "nb": n - len(meas)}], maj_c),
        "conso_estimee_l": _x("Conso estimée (CPH)", "Somme, site par site, de : running time du GE × consommation horaire (CPH) selon la charge.", [
            _v("Total", k["conso_estimee_l"], "L"), _v("Sites au CPH calculé", len(theo)), _v("Running time de ces sites", _f(cph_rt), "h")], [SRC_CPH], per,
            [{"label": "Sites sans CPH calculable (running time, puissance ou type de GE manquant)", "nb": n - len(theo)}], maj_cph,
            cph_note or "Estimation théorique : elle ne s'additionne jamais à la conso mesurée."),
        "ecart_pct": _x("Écart mesuré / estimé", "(Conso mesurée − conso estimée) ÷ conso estimée, sur les seuls sites qui ont les deux.", [
            _v("Conso mesurée (mêmes sites)", k["conso_mesuree_comparable_l"], "L"), _v("Conso estimée (mêmes sites)", k["conso_estimee_comparable_l"], "L"),
            _v("Écart", k["ecart_l"], "L"), _v("Sites comparables", len(both)), _v(f"Sites dont l'écart dépasse {ECART_SEUIL_PCT} %", k["nb_ecart_cph"])],
            [SRC_CAPTEUR, SRC_CPH], per, [{"label": "Sites sans l'une des deux consommations", "nb": n - len(both)}], maj_c,
            "Écart positif : le site consomme plus que ce que le GE devrait brûler (fuite, vol, capteur, compteur d'heures ou type de GE à vérifier)."),
        "runtime_total_h": _x("Running time GE", "Somme des heures de marche des GE du mois.", [
            _v("Total", k["runtime_total_h"], "h"), _v("Sites avec heures", len(rt)), _v("Moyenne par site et par jour", k["h_site_jour"], "h")],
            [SRC_CPH], per, [{"label": "Sites sans heures de marche connues", "nb": n - len(rt)}], maj_cph, cph_note),
        "cph_moyen_l_h": _x("CPH moyen", "Conso estimée totale ÷ running time total, sur les sites au CPH calculé (moyenne pondérée par les heures).", [
            _v("Conso estimée", _f(sum(r["conso_estimee_l"] for r in cph_both)), "L"), _v("Running time", _f(cph_rt), "h"),
            _v("Résultat", k["cph_moyen_l_h"], "L/h")], [SRC_CPH], per, [], maj_cph, cph_note),
        "nb_sites_alerte": _x("Sites en alerte", "Sites ayant au moins une alerte parmi : " + " ; ".join(alertes.values()) + ".", [
            _v(lab, sum(1 for r in rows if code in r["alertes"])) for code, lab in alertes.items()], [SRC_CAPTEUR, SRC_CPH, SRC_TARGET, SRC_STOCK], per, [], maj_c),
    }
    return k, x


def compute_site_table(params, paginate=True) -> dict:
    month = _ref_month(params)
    if not month:
        return {"month": None, "kpis": None, "explain": {}, "rows": [], "total": 0, "page": 1, "page_size": 50, "filters": {"zones": [], "alertes": ALERTES}}
    rows, meta = build_rows(month)
    vue = params.get("vue") or ""
    if vue and vue not in VUES:
        raise _BadPeriod(f"Vue inconnue : {vue}.")
    alertes = {c: ALERTES[c] for c in VUES[vue]} if vue else ALERTES
    if vue:
        for r in rows:
            r["alertes"] = [a for a in r["alertes"] if a in alertes]
            r["nb_alertes"] = len(r["alertes"])
    rows = _filter(rows, params)
    k, x = _kpis(rows, meta, month, alertes)
    sort = params.get("sort") or "nb_alertes"
    if sort not in SORTS:
        raise _BadPeriod(f"Tri inconnu : {sort}.")
    desc = (params.get("desc") or "1") != "0"
    present = [r for r in rows if r.get(sort) is not None]
    rows = sorted(present, key=lambda r: r[sort], reverse=desc) + [r for r in rows if r.get(sort) is None]  # inconnu toujours en fin
    total = len(rows)
    out = {"month": month, "periode": {"start": meta["start"], "end": meta["end"], "nb_jours": meta["nb_jours"]},
           "cph_disponible": meta["cph_disponible"], "kpis": k, "explain": x, "total": total,
           "filters": {"zones": meta["zones"], "alertes": alertes}, "vue": vue or None, "seuils": {"ecart_pct": ECART_SEUIL_PCT, "l_kwh": L_KWH_MAX,
                                                                                "sous_charge_pct": SOUS_CHARGE_PCT, "tolerance_target_pct": meta["tolerance_target_pct"]}}
    if not paginate:
        return {**out, "rows": rows}
    try:
        page, size = max(1, int(params.get("page") or 1)), min(200, max(10, int(params.get("page_size") or 50)))
    except ValueError:
        raise _BadPeriod("Pagination invalide.")
    return {**out, "rows": rows[(page - 1) * size: page * size], "page": page, "page_size": size}


class BiSiteTableView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            return Response(compute_site_table(request.query_params))
        except _BadPeriod as e:
            return Response({"detail": str(e)}, status=400)


def export_site_table(params):
    from django.utils import timezone
    from openpyxl import Workbook

    from fuel_tracking.views_bi import _sheet, _summary_sheet, _xlsx_response

    d = compute_site_table(params, paginate=False)
    k = d["kpis"] or {}
    wb = Workbook()
    _summary_sheet(wb, f"Suivi carburant — Données par site · {d['month'] or '—'}", [
        ("Mois", d["month"] or "—"), ("Zone", params.get("zone") or "Toutes"), ("Recherche", params.get("q") or "—"),
        ("Alerte", ALERTES.get(params.get("alerte") or "", "Toutes")),
    ], [
        ("Sites GE", k.get("nb_sites"), "int"), ("Conso mesurée, L", k.get("conso_mesuree_l"), "L", "conso_mesuree_l"),
        ("Conso estimée (CPH), L", k.get("conso_estimee_l"), "L", "conso_estimee_l"),
        ("Écart mesuré / estimé (mêmes sites), %", k.get("ecart_pct"), "pct", "ecart_pct"),
        ("Running time GE, h", k.get("runtime_total_h"), "L", "runtime_total_h"), ("CPH moyen, L/h", k.get("cph_moyen_l_h"), "dec2", "cph_moyen_l_h"),
        ("Sites en alerte", k.get("nb_sites_alerte"), "int", "nb_sites_alerte"),
    ], d["explain"])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 24), ("Zone", "zone", "txt", 9), ("Typologie", "typologie", "txt", 12),
        ("Indoor / Outdoor", "configuration", "txt", 10), ("Énergie", "energie", "txt", 10), ("GE (kVA)", "ge_kva", "dec1", 8),
        ("Running time (h)", "runtime_h", "dec1", 10), ("Heures / jour", "h_jour", "dec1", 9), ("CPH (L/h)", "cph_l_h", "dec2", 9),
        ("Charge GE (%)", "charge_pct", "pct", 9), ("Statut CPH", "statut_cph", "txt", 12), ("Motif CPH", "motif_cph", "txt", 22),
        ("kWh GE", "ge_prod_kwh", "L", 10),
        ("Conso mesurée (L)", "conso_mesuree_l", "L", 12), ("Jours mesurés", "jours_mesures", "int", 8), ("L / jour", "l_jour", "dec1", 9),
        ("Conso estimée CPH (L)", "conso_estimee_l", "L", 12), ("Écart mesuré − estimé (L)", "ecart_l", "L", 12), ("Écart (%)", "ecart_pct", "pct", 9),
        ("L / kWh", "l_kwh", "ratio", 8), ("Conso mois précédent (L)", "conso_prec_l", "L", 12), ("Évolution L/jour (%)", "evolution_pct", "pct", 10),
        ("Target (L)", "target_l", "L", 10), ("Écart target (%)", "ecart_target_pct", "pct", 10),
        ("Stock (L)", "stock_l", "L", 10), ("Date du stock", "date_stock", "date", 11), ("Source du stock", "source_stock", "txt", 11),
        ("Stock estimé", lambda r: "Oui" if r["stock_estime"] else "", "txt", 8), ("Capacité (L)", "capacite_l", "L", 10),
        ("Remplissage (%)", "remplissage_pct", "pct", 9), ("Autonomie (j)", "autonomie_j", "dec1", 9),
        ("Livré ENOC (L)", "enoc_livre_l", "L", 10), ("Livraisons", "nb_livraisons", "int", 8),
        ("Alertes", lambda r: " ; ".join(ALERTES[a] for a in r["alertes"]), "txt", 40),
    ], d["rows"])
    return _xlsx_response(wb, f"donnees_par_site_{d['month'] or timezone.localdate().isoformat()}.xlsx")
