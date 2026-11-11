"""
Vues « BI » du Suivi carburant (graphiques, KPI, classements) et exports Excel — lecture seule.

  GET /api/fuel-tracking/bi/conso/?from_month=YYYY-MM&to_month=YYYY-MM&country=&typology=
  GET /api/fuel-tracking/bi/stock/?country=&month=YYYY-MM
  GET /api/fuel-tracking/bi/cph/?start=YYYY-MM-DD&end=YYYY-MM-DD&country=
  GET /api/fuel-tracking/bi/export/<conso|stock|cph|commande|estimation>/?<mêmes filtres>   → .xlsx

Règles (CLAUDE.md) : périmètre « avec GE » identique aux autres écrans ; mesuré et estimé jamais
additionnés ; une valeur absente reste null (vide dans Excel, jamais 0) ; ratios = ratio des sommes.
Les écrans et les exports partagent les mêmes fonctions de calcul : un chiffre exporté est celui affiché.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date
from io import BytesIO

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

TOP_N = 10
# En dessous, le ratio L/kWh d'un site est trop bruité pour un classement.
MIN_KWH_FOR_RATIO = 100
QUALITY_ORDER = ["OK", "LOW_QUALITY", "NO_VALID_LEVEL"]


def _f(v, places=1):
    return round(float(v), places) if v is not None else None


def _ratio(num, den, places=3):
    return round(float(num) / float(den), places) if num is not None and den not in (None, 0) else None


def _pct_change(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round((float(cur) - float(prev)) / float(prev) * 100, 1)


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _days_in(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


# ─── Consommation ────────────────────────────────────────────────────────────

def _conso_base(params):
    from fuel_tracking.models import FuelConsommationMonthly
    from fuel_tracking.views import _effective_ge_q, _file_ge_site_ids

    return FuelConsommationMonthly.objects.filter(_effective_ge_q(_file_ge_site_ids())).annotate(
        typo=Coalesce("typology_fichier", "typology", Value(None)))


def _totals(qs):
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    a = qs.aggregate(
        nb_sites=Count("site_id", distinct=True),
        nb_sites_mesure=Count("site_id", distinct=True, filter=Q(conso_snowflake_l__isnull=False)),
        conso_mesuree_l=Sum("conso_snowflake_l"),
        # L/site/jour = conso ÷ jours-site réellement mesurés (pas jours calendaires) : n'invente pas de données.
        jours_site_mesures=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
        enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
        nb_ravitaillements=Sum("enoc_nb_demandes"),
        num=Sum("conso_snowflake_l", filter=both),
        den=Sum("ge_prod_kwh", filter=both),
    )
    return {
        "nb_sites": a["nb_sites"],
        "nb_sites_mesure": a["nb_sites_mesure"],
        "couverture_pct": round(a["nb_sites_mesure"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
        "conso_mesuree_l": _f(a["conso_mesuree_l"]),
        "l_site_jour": _ratio(a["conso_mesuree_l"], a["jours_site_mesures"], 1),
        "enoc_ajoutee_l": _f(a["enoc_ajoutee_l"]),
        "nb_ravitaillements": a["nb_ravitaillements"],
        "l_kwh": _ratio(a["num"], a["den"]),
    }


def _breakdown(qs, field):
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    rows = (
        qs.values(key=F(field))
        .annotate(
            nb_sites=Count("site_id", distinct=True),
            conso_mesuree_l=Sum("conso_snowflake_l"),
            jours=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
            enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
            num=Sum("conso_snowflake_l", filter=both),
            den=Sum("ge_prod_kwh", filter=both),
        )
        .order_by(F("conso_mesuree_l").desc(nulls_last=True))
    )
    return [{
        "label": r["key"] or "Non renseigné", "nb_sites": r["nb_sites"],
        "conso_mesuree_l": _f(r["conso_mesuree_l"]), "l_site_jour": _ratio(r["conso_mesuree_l"], r["jours"], 1),
        "enoc_ajoutee_l": _f(r["enoc_ajoutee_l"]), "l_kwh": _ratio(r["num"], r["den"]),
    } for r in rows]


def _per_site(period, months, targets, tolerance):
    """Agrégat par site sur la période — base des classements ET de la feuille « Sites » de l'export."""
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    rows = period.values("site_id").annotate(
        conso=Sum("conso_snowflake_l"), enoc=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
        nb_ravit=Sum("enoc_nb_demandes"), jours=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
        num=Sum("conso_snowflake_l", filter=both), den=Sum("ge_prod_kwh", filter=both),
        kwh=Sum("ge_prod_kwh"), nb_mois=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
    )
    info = {r["site_id"]: r for r in period.order_by("month_year").values("site_id", "site_name", "country", "typo")}
    out = []
    for r in rows:
        i = info.get(r["site_id"], {})
        t = targets.get((r["site_id"] or "").upper())
        # Target comparé seulement sur les mois où la conso est mesurée (target mensuel × mois mesurés).
        target_l = float(t.target_mensuel_l) * r["nb_mois"] if t is not None and r["nb_mois"] else None
        conso = float(r["conso"]) if r["conso"] is not None else None
        ecart_t = round((conso - target_l) / target_l * 100, 1) if conso is not None and target_l else None
        out.append({
            "site_id": r["site_id"], "site_name": i.get("site_name"), "country": i.get("country"), "typologie": i.get("typo"),
            "nb_mois_mesures": r["nb_mois"], "jours_mesures": r["jours"],
            "conso_mesuree_l": _f(conso), "l_site_jour": _ratio(r["conso"], r["jours"], 1),
            "enoc_ajoutee_l": _f(r["enoc"]), "nb_ravitaillements": r["nb_ravit"],
            "ecart_enoc_l": _f(conso - float(r["enoc"])) if conso is not None and r["enoc"] is not None else None,
            "ge_prod_kwh": _f(r["kwh"]), "l_kwh": _ratio(r["num"], r["den"]) if r["den"] and float(r["den"]) >= MIN_KWH_FOR_RATIO else None,
            "target_l": _f(target_l), "ecart_target_pct": ecart_t,
            "statut_target": None if ecart_t is None else ("Au-dessus" if ecart_t > tolerance else "Sous" if ecart_t < -tolerance else "Dans la tolérance"),
            "target_confirme": bool(t is not None and t.confirmed_year),
        })
    return out


def compute_conso(params) -> dict:
    from fuel_tracking.services.fuel_target import active_targets, tolerance_pct

    base = _conso_base(params)
    all_months = list(base.order_by("month_year").values_list("month_year", flat=True).distinct())
    countries = sorted({c for c in base.values_list("country", flat=True).distinct() if c})
    typologies = sorted({t for t in base.values_list("typo", flat=True).distinct() if t})
    if not all_months:
        return {"months": [], "available_months": [], "filters": {"countries": [], "typologies": []}, "sites": []}

    to_month = (params.get("to_month") or "").strip() or all_months[-1]
    from_month = (params.get("from_month") or "").strip() or _shift_month(to_month, -5)
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    if params.get("country"):
        base = base.filter(country__iexact=params["country"])
    if params.get("typology"):
        base = base.filter(typo=params["typology"])

    months = [m for m in all_months if from_month <= m <= to_month]
    span = len(months) or 1
    period = base.filter(month_year__in=months)
    prev_months = [m for m in all_months if _shift_month(from_month, -span) <= m < from_month]
    prev = base.filter(month_year__in=prev_months)

    targets, tolerance = active_targets(), tolerance_pct()
    monthly = []
    for m in months:
        mq = period.filter(month_year=m)
        t = _totals(mq)
        # Target du mois : sites ayant à la fois une conso mesurée et un target actif.
        t_sum = c_sum = 0.0
        for sid, c in mq.filter(conso_snowflake_l__isnull=False).values_list("site_id", "conso_snowflake_l"):
            tg = targets.get((sid or "").upper())
            if tg is not None:
                t_sum += float(tg.target_mensuel_l)
                c_sum += float(c)
        q = dict(mq.values_list("quality_status").annotate(n=Count("id")))
        quality = {k: q.pop(k, 0) for k in QUALITY_ORDER}
        quality["Autre / absent"] = sum(q.values())
        monthly.append({"month": m, **t, "target_l": _f(t_sum) if t_sum else None,
                        "conso_vs_target_l": _f(c_sum) if t_sum else None, "qualite": quality})

    cur_t = _totals(period)
    prev_t = _totals(prev) if prev_months else None
    deltas = {k: _pct_change(cur_t[k], prev_t[k]) if prev_t else None
              for k in ("conso_mesuree_l", "enoc_ajoutee_l", "l_kwh", "l_site_jour")}
    if prev_t and cur_t["couverture_pct"] is not None and prev_t["couverture_pct"] is not None:
        deltas["couverture_pts"] = round(cur_t["couverture_pct"] - prev_t["couverture_pct"], 1)

    sites = _per_site(period, months, targets, tolerance)
    with_target = [s for s in sites if s["ecart_target_pct"] is not None]
    t_tot = sum(s["target_l"] for s in with_target)
    c_tot = sum(s["conso_mesuree_l"] for s in with_target)
    cur_t.update({
        "nb_sites_target": len(with_target),
        "ecart_target_pct": _pct_change(c_tot, t_tot) if with_target else None,
        "nb_sites_au_dessus_target": sum(1 for s in with_target if s["statut_target"] == "Au-dessus"),
        "nb_targets_a_confirmer": sum(1 for s in with_target if not s["target_confirme"]),
        "tolerance_target_pct": tolerance,
    })

    ratios = [s for s in sites if s["l_kwh"] is not None]
    buckets = [("< 0,20", None, 0.20), ("0,20–0,25", 0.20, 0.25), ("0,25–0,30", 0.25, 0.30),
               ("0,30–0,35", 0.30, 0.35), ("0,35–0,45", 0.35, 0.45), ("> 0,45", 0.45, None)]
    dist = [{"label": label, "nb_sites": sum(1 for s in ratios if (lo is None or s["l_kwh"] >= lo) and (hi is None or s["l_kwh"] < hi)),
             "reference": lo == 0.25} for label, lo, hi in buckets]

    def top(rows, key, reverse=True):
        return sorted(rows, key=key, reverse=reverse)[:TOP_N]

    return {
        "from_month": months[0] if months else from_month, "to_month": months[-1] if months else to_month,
        "months": months, "available_months": all_months, "previous_months": prev_months,
        "filters": {"countries": countries, "typologies": typologies},
        "kpis": cur_t, "deltas": deltas, "monthly": monthly,
        "by_country": _breakdown(period, "country"), "by_typology": _breakdown(period, "typo"),
        "top_conso": top([s for s in sites if s["conso_mesuree_l"] is not None], lambda s: s["conso_mesuree_l"]),
        "top_target": top([s for s in with_target if s["statut_target"] == "Au-dessus"], lambda s: s["ecart_target_pct"]),
        "top_ecart_enoc": top([s for s in sites if s["ecart_enoc_l"] is not None], lambda s: abs(s["ecart_enoc_l"])),
        "top_l_kwh": top(ratios, lambda s: s["l_kwh"]),
        "distribution_l_kwh": dist, "reference_l_kwh": [0.25, 0.30],
        "sites": sites,
    }


class BiConsoView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = compute_conso(request.query_params)
        data.pop("sites", None)  # liste complète réservée à l'export Excel
        return Response(data)


# ─── Stock ───────────────────────────────────────────────────────────────────

def compute_stock(params) -> dict:
    from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot

    qs = FuelStockSnapshot.objects.filter(snapshot_year__isnull=True, has_genset=True)
    countries = sorted({c for c in qs.values_list("country", flat=True).distinct() if c})
    if params.get("country"):
        qs = qs.filter(country__iexact=params["country"])

    a = qs.aggregate(
        nb_sites=Count("id"),
        nb_stock_connu=Count("id", filter=Q(stock_snowflake_l__isnull=False) | Q(stock_enoc_l__isnull=False)),
        stock_l=Sum("stock_snowflake_l"), capacite_l=Sum("capacity_snowflake_l", filter=Q(stock_snowflake_l__isnull=False)),
        critique=Count("id", filter=Q(stock_snowflake_pct__lt=15)),
        alerte=Count("id", filter=Q(stock_snowflake_pct__gte=15, stock_snowflake_pct__lt=40)),
        moyen=Count("id", filter=Q(stock_snowflake_pct__gte=40, stock_snowflake_pct__lt=70)),
        bon=Count("id", filter=Q(stock_snowflake_pct__gte=70)),
        sans_pct=Count("id", filter=Q(stock_snowflake_pct__isnull=True)),
    )
    last_date = qs.exclude(stock_snowflake_date__isnull=True).order_by("-stock_snowflake_date") \
        .values_list("stock_snowflake_date", flat=True).first()

    # Autonomie = stock actuel ÷ conso mesurée moyenne par jour mesuré du mois de référence (dernier mois complet).
    months = list(FuelConsommationMonthly.objects.order_by("-month_year").values_list("month_year", flat=True).distinct()[:2])
    ref_month = (params.get("month") or "").strip() or (months[1] if len(months) > 1 else (months[0] if months else None))
    daily = {}
    if ref_month:
        for sid, c, j in FuelConsommationMonthly.objects.filter(month_year=ref_month, conso_snowflake_l__gt=0) \
                .values_list("site_id", "conso_snowflake_l", "nb_jours_data"):
            daily[sid] = float(c) / (j or _days_in(ref_month))

    today = timezone.localdate()
    sites = []
    for r in qs.values("site_id", "site_name", "country", "typology", "stock_snowflake_l", "capacity_snowflake_l",
                       "stock_snowflake_pct", "stock_snowflake_date", "stock_enoc_l", "stock_enoc_date"):
        d = daily.get(r["site_id"])
        sites.append({
            "site_id": r["site_id"], "site_name": r["site_name"], "country": r["country"], "typologie": r["typology"],
            "stock_l": _f(r["stock_snowflake_l"]), "capacite_l": _f(r["capacity_snowflake_l"]),
            "remplissage_pct": _f(r["stock_snowflake_pct"]), "date": r["stock_snowflake_date"],
            "age_releve_j": (today - r["stock_snowflake_date"]).days if r["stock_snowflake_date"] else None,
            "stock_enoc_l": _f(r["stock_enoc_l"]), "date_enoc": r["stock_enoc_date"],
            "conso_jour_l": round(d, 1) if d else None,
            "autonomie_j": round(float(r["stock_snowflake_l"]) / d, 1) if d and r["stock_snowflake_l"] is not None else None,
        })

    with_pct = [s for s in sites if s["remplissage_pct"] is not None]
    with_auto = [s for s in sites if s["autonomie_j"] is not None]
    auto_buckets = [("< 3 j", 0, 3), ("3–7 j", 3, 7), ("7–15 j", 7, 15), ("15–30 j", 15, 30), ("> 30 j", 30, None)]
    age_buckets = [("≤ 1 j", 0, 2), ("2–3 j", 2, 4), ("4–7 j", 4, 8), ("8–15 j", 8, 16), ("> 15 j", 16, None)]
    by_country = defaultdict(lambda: {"nb_sites": 0, "stock_l": 0.0, "capacite_l": 0.0, "nb_critique": 0, "has": False})
    for s in sites:
        b = by_country[s["country"] or "Non renseigné"]
        b["nb_sites"] += 1
        if s["stock_l"] is not None:
            b["has"] = True
            b["stock_l"] += s["stock_l"]
            b["capacite_l"] += s["capacite_l"] or 0
        if s["remplissage_pct"] is not None and s["remplissage_pct"] < 15:
            b["nb_critique"] += 1

    return {
        "filters": {"countries": countries},
        "kpis": {
            "nb_sites": a["nb_sites"], "nb_stock_connu": a["nb_stock_connu"],
            "stock_connu_pct": round(a["nb_stock_connu"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
            "stock_total_l": _f(a["stock_l"]), "capacite_l": _f(a["capacite_l"]),
            "remplissage_pct": round(float(a["stock_l"]) / float(a["capacite_l"]) * 100, 1) if a["stock_l"] is not None and a["capacite_l"] else None,
            "nb_critique": a["critique"], "nb_alerte": a["alerte"],
            "nb_autonomie_moins_3j": sum(1 for s in with_auto if s["autonomie_j"] < 3),
            "autonomie_mediane_j": sorted(s["autonomie_j"] for s in with_auto)[len(with_auto) // 2] if with_auto else None,
            "nb_releves_anciens": sum(1 for s in sites if s["age_releve_j"] is not None and s["age_releve_j"] > 7),
            "derniere_mesure": last_date, "mois_reference_autonomie": ref_month,
        },
        "distribution_remplissage": [
            {"label": "< 15 %", "nb_sites": a["critique"], "niveau": "critique"},
            {"label": "15–40 %", "nb_sites": a["alerte"], "niveau": "alerte"},
            {"label": "40–70 %", "nb_sites": a["moyen"], "niveau": "moyen"},
            {"label": "> 70 %", "nb_sites": a["bon"], "niveau": "bon"},
            {"label": "Inconnu", "nb_sites": a["sans_pct"], "niveau": "inconnu"},
        ],
        "distribution_autonomie": [
            {"label": label, "nb_sites": sum(1 for s in with_auto if s["autonomie_j"] >= lo and (hi is None or s["autonomie_j"] < hi))}
            for label, lo, hi in auto_buckets
        ],
        "distribution_age_releve": [
            {"label": label, "nb_sites": sum(1 for s in sites if s["age_releve_j"] is not None and s["age_releve_j"] >= lo and (hi is None or s["age_releve_j"] < hi))}
            for label, lo, hi in age_buckets
        ] + [{"label": "Aucun relevé", "nb_sites": sum(1 for s in sites if s["age_releve_j"] is None)}],
        "by_country": sorted([{
            "label": k, "nb_sites": b["nb_sites"], "stock_l": round(b["stock_l"], 1) if b["has"] else None,
            "remplissage_pct": round(b["stock_l"] / b["capacite_l"] * 100, 1) if b["has"] and b["capacite_l"] else None,
            "nb_critique": b["nb_critique"]} for k, b in by_country.items()], key=lambda x: -x["nb_sites"]),
        "top_critique": sorted(with_pct, key=lambda s: s["remplissage_pct"])[:TOP_N],
        "top_autonomie_faible": sorted(with_auto, key=lambda s: s["autonomie_j"])[:TOP_N],
        "top_stock": sorted((s for s in sites if s["stock_l"] is not None), key=lambda s: -s["stock_l"])[:TOP_N],
        "sites": sites,
    }


class BiStockView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = compute_stock(request.query_params)
        data.pop("sites", None)
        return Response(data)


# ─── CPH ─────────────────────────────────────────────────────────────────────

class _BadPeriod(ValueError):
    pass


def compute_cph(params) -> dict:
    from fuel_tracking.services.cph_service import compute_period_summary
    from fuel_tracking.views_cph import _parse_period, _periode_info

    start, end = _parse_period(params)
    if end < start:
        start, end = end, start
    if (end - start).days > 92:
        raise _BadPeriod("Période CPH limitée à 93 jours.")
    rows = compute_period_summary(start, end, country=params.get("country") or None)

    ge = [r for r in rows if r.get("perimetre") == "GE"]
    meas = lambda r: (r.get("comparaison") or {}).get("conso_mesuree_l")  # noqa: E731
    theo = [r for r in ge if r.get("conso_theorique_l") is not None]
    rt = [r for r in ge if r.get("runtime_total_h") is not None]
    both = [r for r in theo if meas(r) is not None]

    status_counts, motifs, by_country = defaultdict(int), defaultdict(int), {}
    for r in ge:
        status_counts[r.get("cph_status") or "NON_CALCULE"] += 1
        if r.get("cph_status") != "COMPLET":
            motifs[((r.get("motif_cph") or {}).get("code")) or "AUTRE"] += 1
        k = r.get("country") or "Non renseigné"
        b = by_country.setdefault(k, {"label": k, "nb_sites": 0, "nb_cph_complet": 0, "conso_theorique_l": 0.0, "runtime_h": 0.0})
        b["nb_sites"] += 1
        if r.get("conso_theorique_l") is not None:
            b["nb_cph_complet"] += 1
            b["conso_theorique_l"] += float(r["conso_theorique_l"])
        if r.get("runtime_total_h") is not None:
            b["runtime_h"] += float(r["runtime_total_h"])
    for b in by_country.values():
        b["conso_theorique_l"] = round(b["conso_theorique_l"], 1) if b["nb_cph_complet"] else None
        b["runtime_h"] = round(b["runtime_h"], 1)

    sites = []
    for r in ge:
        m, t = meas(r), r.get("conso_theorique_l")
        sites.append({
            "site_id": r["site_id"], "site_name": r.get("site_name"), "country": r.get("country"), "zone": r.get("zone"),
            "configuration": r.get("kind"), "ge_kva": _f(r.get("ge_kva")), "statut_cph": r.get("cph_status"),
            "motif": (r.get("motif_cph") or {}).get("code"), "runtime_h": _f(r.get("runtime_total_h")),
            "source_runtime": r.get("runtime_source_main"), "charge_pct": _f(float(r["charge_moy"]) * 100) if r.get("charge_moy") is not None else None,
            "p_ge_kw": _f(r.get("p_ge_moy_kw"), 2), "cph_l_h": _f(r.get("cph_moy_l_h"), 2),
            "conso_theorique_l": _f(t), "conso_partielle_l": _f(r.get("conso_partielle_l")), "conso_mesuree_l": _f(m),
            "ecart_l": _f(float(m) - float(t)) if m is not None and t is not None else None,
            "ecart_pct": _pct_change(m, t) if m is not None and t is not None else None,
        })
    by_id = {s["site_id"]: s for s in sites}
    sum_theo = sum(float(r["conso_theorique_l"]) for r in both)
    sum_meas = sum(float(meas(r)) for r in both)
    theo_rt = sum(float(r["runtime_total_h"]) for r in theo if r.get("runtime_total_h") is not None)

    return {
        "start": start, "end": end, "periode": _periode_info(start, end),
        "kpis": {
            "nb_sites_ge": len(ge), "nb_cph_complet": len(theo),
            "couverture_cph_pct": round(len(theo) / len(ge) * 100, 1) if ge else None,
            "conso_theorique_l": round(sum(float(r["conso_theorique_l"]) for r in theo), 1) if theo else None,
            "runtime_total_h": round(sum(float(r["runtime_total_h"]) for r in rt), 1) if rt else None,
            "cph_moy_l_h": _ratio(sum(float(r["conso_theorique_l"]) for r in theo), theo_rt or None, 2) if theo else None,
            "charge_moy_pct": _f(sum(float(r["charge_moy"]) * float(r["runtime_total_h"]) for r in rt if r.get("charge_moy") is not None)
                                 / sum(float(r["runtime_total_h"]) for r in rt if r.get("charge_moy") is not None) * 100)
            if any(r.get("charge_moy") is not None and float(r["runtime_total_h"]) > 0 for r in rt) else None,
            "nb_sites_comparables": len(both),
            "conso_theorique_comparable_l": round(sum_theo, 1) if both else None,
            "conso_mesuree_comparable_l": round(sum_meas, 1) if both else None,
            "ecart_global_pct": _pct_change(sum_meas, sum_theo) if both else None,
        },
        "statuts_cph": [{"label": k, "nb_sites": v} for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])],
        "motifs_non_calcule": [{"label": k, "nb_sites": v} for k, v in sorted(motifs.items(), key=lambda kv: -kv[1])[:8]],
        "by_country": sorted(by_country.values(), key=lambda b: -b["nb_sites"]),
        "charge_vs_cph": [{"site_id": s["site_id"], "charge_pct": s["charge_pct"], "cph_l_h": s["cph_l_h"]}
                          for s in sites if s["charge_pct"] is not None and s["cph_l_h"] is not None][:800],
        "top_conso_theorique": [by_id[r["site_id"]] for r in sorted(theo, key=lambda r: -float(r["conso_theorique_l"]))[:TOP_N]],
        "top_ecart_mesure": [by_id[r["site_id"]] for r in sorted(both, key=lambda r: -abs(float(meas(r)) - float(r["conso_theorique_l"])))[:TOP_N]],
        "top_runtime": [by_id[r["site_id"]] for r in sorted(rt, key=lambda r: -float(r["runtime_total_h"]))[:TOP_N]],
        "sites": sites,
    }


class BiCphView(APIView):
    """Vue BI sur le calcul CPH de la période (même calcul mis en cache que l'onglet Contrôle CPH)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            data = compute_cph(request.query_params)
        except (_BadPeriod, ValueError) as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:  # source indisponible : l'écran affiche « indisponible », jamais 0
            return Response({"detail": f"Calcul CPH indisponible ({type(e).__name__})."}, status=503)
        data.pop("sites", None)
        return Response(data)


# ─── Export Excel ────────────────────────────────────────────────────────────

NAVY, BLUE_L, GREY = "0B1F4D", "E4EFFE", "F5F7FA"

FMT = {"L": "#,##0", "pct": "0.0", "ratio": "0.000", "dec1": "#,##0.0", "dec2": "0.00", "int": "#,##0", "date": "dd/mm/yyyy", "txt": "@"}


def _sheet(wb, title, columns, rows, first=False):
    """Feuille tableau : en-tête Camusat, filtres auto, volets figés, formats numériques, cellules vides si absent."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ws = wb.active if first else wb.create_sheet()
    ws.title = title[:31]
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
    thin = Side(style="thin", color="D9DEE7")
    for ci, (label, _key, _fmt, width) in enumerate(columns, 1):
        c = ws.cell(row=1, column=ci, value=label)
        c.font, c.fill = head_font, head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 32
    for ri, row in enumerate(rows, 2):
        for ci, (_label, key, fmt, _w) in enumerate(columns, 1):
            v = key(row) if callable(key) else row.get(key)
            c = ws.cell(row=ri, column=ci, value=v)
            if v is not None and fmt in FMT:
                c.number_format = FMT[fmt]
    ws.freeze_panes = "B2" if len(columns) > 3 else "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    return ws


def _summary_sheet(wb, title, filters: list[tuple[str, str]], kpis: list[tuple[str, object, str]]):
    from openpyxl.styles import Font, PatternFill

    ws = wb.active
    ws.title = "Synthèse"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = f"Export du {timezone.localtime():%d/%m/%Y à %H:%M} — EnerTrack, Suivi carburant"
    ws["A2"].font = Font(italic=True, color="64748B")
    r = 4
    for label, value in filters:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=2, value=value)
        r += 1
    r += 1
    ws.cell(row=r, column=1, value="Indicateur").font = Font(bold=True, color="FFFFFF")
    ws.cell(row=r, column=2, value="Valeur").font = Font(bold=True, color="FFFFFF")
    for col in (1, 2):
        ws.cell(row=r, column=col).fill = PatternFill("solid", fgColor=NAVY)
    for label, value, fmt in kpis:
        r += 1
        ws.cell(row=r, column=1, value=label)
        c = ws.cell(row=r, column=2, value=value if value is not None else "Non disponible")
        if value is not None and fmt in FMT:
            c.number_format = FMT[fmt]
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 26
    return ws


def _xlsx_response(wb, name):
    buf = BytesIO()
    wb.save(buf)
    r = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    r["Content-Disposition"] = f'attachment; filename="{name}"'
    return r


def _export_conso(p):
    from openpyxl import Workbook

    d = compute_conso(p)
    k = d.get("kpis") or {}
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Consommation", [
        ("Période", f"{d.get('from_month', '—')} → {d.get('to_month', '—')}"),
        ("Pays", p.get("country") or "Tous"), ("Typologie", p.get("typology") or "Toutes"),
        ("Périmètre", "Sites avec groupe électrogène"),
    ], [
        ("Conso mesurée (capteur), L", k.get("conso_mesuree_l"), "L"),
        ("Conso moyenne, L / site / jour mesuré", k.get("l_site_jour"), "dec1"),
        ("Conso spécifique, L/kWh", k.get("l_kwh"), "ratio"),
        ("Ajouté ENOC, L", k.get("enoc_ajoutee_l"), "L"),
        ("Demandes de ravitaillement ENOC", k.get("nb_ravitaillements"), "int"),
        ("Sites GE", k.get("nb_sites"), "int"), ("Sites avec conso mesurée", k.get("nb_sites_mesure"), "int"),
        ("Couverture capteur, %", k.get("couverture_pct"), "pct"),
        ("Écart conso vs target, % (sites avec target)", k.get("ecart_target_pct"), "pct"),
        ("Sites au-dessus du target", k.get("nb_sites_au_dessus_target"), "int"),
        ("Targets encore à confirmer", k.get("nb_targets_a_confirmer"), "int"),
    ])
    _sheet(wb, "Mensuel", [
        ("Mois", "month", "txt", 10), ("Sites GE", "nb_sites", "int", 10), ("Sites mesurés", "nb_sites_mesure", "int", 12),
        ("Couverture %", "couverture_pct", "pct", 12), ("Conso mesurée (L)", "conso_mesuree_l", "L", 16),
        ("L/site/jour", "l_site_jour", "dec1", 11), ("L/kWh", "l_kwh", "ratio", 9), ("Ajouté ENOC (L)", "enoc_ajoutee_l", "L", 15),
        ("Target (L)", "target_l", "L", 13), ("Conso des sites avec target (L)", "conso_vs_target_l", "L", 18),
        ("Qualité OK", lambda r: r["qualite"]["OK"], "int", 11), ("Qualité faible", lambda r: r["qualite"]["LOW_QUALITY"], "int", 12),
        ("Niveau invalide", lambda r: r["qualite"]["NO_VALID_LEVEL"], "int", 13), ("Autre / absent", lambda r: r["qualite"]["Autre / absent"], "int", 13),
    ], d.get("monthly") or [])
    bd_cols = [("", "label", "txt", 22), ("Sites", "nb_sites", "int", 9), ("Conso mesurée (L)", "conso_mesuree_l", "L", 16),
               ("L/site/jour", "l_site_jour", "dec1", 11), ("L/kWh", "l_kwh", "ratio", 9), ("Ajouté ENOC (L)", "enoc_ajoutee_l", "L", 15)]
    _sheet(wb, "Par pays", [("Pays", *bd_cols[0][1:])] + bd_cols[1:], d.get("by_country") or [])
    _sheet(wb, "Par typologie", [("Typologie", *bd_cols[0][1:])] + bd_cols[1:], d.get("by_typology") or [])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Pays", "country", "txt", 11), ("Typologie", "typologie", "txt", 14),
        ("Mois mesurés", "nb_mois_mesures", "int", 9), ("Jours mesurés", "jours_mesures", "int", 9),
        ("Conso mesurée (L)", "conso_mesuree_l", "L", 15), ("L/site/jour", "l_site_jour", "dec1", 10),
        ("kWh GE", "ge_prod_kwh", "L", 11), ("L/kWh", "l_kwh", "ratio", 8),
        ("Ajouté ENOC (L)", "enoc_ajoutee_l", "L", 14), ("Demandes ENOC", "nb_ravitaillements", "int", 10), ("Écart mesuré − ENOC (L)", "ecart_enoc_l", "L", 15),
        ("Target période (L)", "target_l", "L", 14), ("Écart vs target %", "ecart_target_pct", "pct", 11),
        ("Statut target", "statut_target", "txt", 15), ("Target confirmé", lambda r: "Oui" if r["target_confirme"] else ("Non" if r["target_l"] is not None else None), "txt", 10),
    ], sorted(d.get("sites") or [], key=lambda s: -(s["conso_mesuree_l"] or 0)))
    return _xlsx_response(wb, f"suivi_conso_{d.get('from_month', '')}_{d.get('to_month', '')}.xlsx")


def _export_stock(p):
    from openpyxl import Workbook

    d = compute_stock(p)
    k = d["kpis"]
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Stock actuel", [
        ("Pays", p.get("country") or "Tous"), ("Dernier relevé capteur", str(k.get("derniere_mesure") or "—")),
        ("Mois de référence de l'autonomie", k.get("mois_reference_autonomie") or "—"),
    ], [
        ("Stock total capteur, L", k.get("stock_total_l"), "L"), ("Remplissage global, %", k.get("remplissage_pct"), "pct"),
        ("Sites GE", k.get("nb_sites"), "int"), ("Sites au stock connu", k.get("nb_stock_connu"), "int"),
        ("Cuves critiques (< 15 %)", k.get("nb_critique"), "int"), ("Cuves en alerte (15–40 %)", k.get("nb_alerte"), "int"),
        ("Autonomie médiane, jours", k.get("autonomie_mediane_j"), "dec1"), ("Sites à moins de 3 jours d'autonomie", k.get("nb_autonomie_moins_3j"), "int"),
        ("Relevés de plus de 7 jours", k.get("nb_releves_anciens"), "int"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Pays", "country", "txt", 11), ("Typologie", "typologie", "txt", 14),
        ("Stock capteur (L)", "stock_l", "L", 13), ("Capacité (L)", "capacite_l", "L", 12), ("Remplissage %", "remplissage_pct", "pct", 12),
        ("Date relevé", "date", "date", 12), ("Âge du relevé (j)", "age_releve_j", "int", 11),
        ("Stock ENOC (L)", "stock_enoc_l", "L", 12), ("Date ENOC", "date_enoc", "date", 12),
        ("Conso / jour (L)", "conso_jour_l", "dec1", 11), ("Autonomie (j)", "autonomie_j", "dec1", 11),
    ], sorted(d["sites"], key=lambda s: (s["remplissage_pct"] is None, s["remplissage_pct"] or 0)))
    _sheet(wb, "Par pays", [("Pays", "label", "txt", 18), ("Sites", "nb_sites", "int", 9), ("Stock (L)", "stock_l", "L", 13),
                            ("Remplissage %", "remplissage_pct", "pct", 12), ("Cuves critiques", "nb_critique", "int", 12)], d["by_country"])
    return _xlsx_response(wb, f"suivi_stock_{timezone.localdate():%Y-%m-%d}.xlsx")


def _export_cph(p):
    from openpyxl import Workbook

    d = compute_cph(p)
    k = d["kpis"]
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Contrôle CPH", [
        ("Période", f"{d['start']:%d/%m/%Y} → {d['end']:%d/%m/%Y}"), ("Pays", p.get("country") or "Tous"),
    ], [
        ("Sites GE", k["nb_sites_ge"], "int"), ("Sites au CPH complet", k["nb_cph_complet"], "int"),
        ("Couverture CPH complète, %", k["couverture_cph_pct"], "pct"), ("Conso estimée CPH, L", k["conso_theorique_l"], "L"),
        ("Heures de marche GE", k["runtime_total_h"], "dec1"), ("CPH moyen, L/h", k["cph_moy_l_h"], "dec2"),
        ("Charge moyenne GE, %", k["charge_moy_pct"], "pct"),
        ("Sites comparables (CPH et mesuré)", k["nb_sites_comparables"], "int"),
        ("Écart mesuré vs CPH, % (sites comparables)", k["ecart_global_pct"], "pct"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Pays", "country", "txt", 11), ("Zone", "zone", "txt", 11),
        ("Configuration", "configuration", "txt", 11), ("GE (kVA)", "ge_kva", "dec1", 9), ("Statut CPH", "statut_cph", "txt", 12),
        ("Motif", "motif", "txt", 22), ("Heures de marche", "runtime_h", "dec1", 11), ("Source heures", "source_runtime", "txt", 12),
        ("Charge %", "charge_pct", "pct", 9), ("P GE (kW)", "p_ge_kw", "dec2", 9), ("CPH (L/h)", "cph_l_h", "dec2", 9),
        ("Conso CPH (L)", "conso_theorique_l", "L", 12), ("Conso CPH partielle (L)", "conso_partielle_l", "L", 13),
        ("Conso mesurée (L)", "conso_mesuree_l", "L", 13), ("Écart mesuré − CPH (L)", "ecart_l", "L", 13), ("Écart %", "ecart_pct", "pct", 9),
    ], d["sites"])
    _sheet(wb, "Motifs", [("Motif", "label", "txt", 30), ("Sites", "nb_sites", "int", 10)], d["motifs_non_calcule"])
    return _xlsx_response(wb, f"controle_cph_{d['start']:%Y-%m-%d}_{d['end']:%Y-%m-%d}.xlsx")


def _export_commande(p):
    from openpyxl import Workbook

    from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite

    month = (p.get("month") or "").strip() or FuelSuiviCommandeSite.objects.order_by("-month_year").values_list("month_year", flat=True).first()
    rows = list(FuelSuiviCommandeSite.objects.filter(month_year=month).order_by("-commande_avec_marge_l").values()) if month else []
    wb = Workbook()
    tot = lambda f: _f(sum(float(r[f] or 0) for r in rows)) if rows else None  # noqa: E731
    _summary_sheet(wb, "Suivi carburant — Commande Ops", [("Mois", month or "—"), ("Source", "Fichier de commande Ops importé")], [
        ("Sites du fichier", len(rows), "int"), ("Commande avec marge, L", tot("commande_avec_marge_l"), "L"),
        ("Commande sans marge, L", tot("commande_sans_marge_l"), "L"),
        ("Sites avec commande > 0", sum(1 for r in rows if (r["commande_avec_marge_l"] or 0) > 0), "int"),
        ("Sites au stock final estimé négatif", sum(1 for r in rows if (r["estimation_stock_final_l"] or 0) < 0), "int"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Batch", "batch", "txt", 12),
        ("Typologie contractuelle", "typologie_contractuelle", "txt", 16), ("Typologie facturée", "typologie_facturee", "txt", 16),
        ("Indoor / Outdoor", "indoor_outdoor", "txt", 10), ("Load", "load_commande", "dec2", 8), ("Conso moy. / jour (L)", "conso_moy_jour_l", "dec1", 12),
        ("Commande sans marge (L)", "commande_sans_marge_l", "L", 14), ("Commande avec marge (L)", "commande_avec_marge_l", "L", 14),
        ("Stock final estimé (L)", "estimation_stock_final_l", "L", 14),
    ], [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in rows])
    try:
        syn = list(FuelCommandeSynthese.objects.filter(month_year=month).values())
    except Exception:
        syn = []
    if syn:
        cols = [k for k in syn[0].keys() if k not in ("id", "month_year", "imported_at", "source_filename")]
        _sheet(wb, "Synthèse Ops", [(c.replace("_", " ").capitalize(), c, "txt" if isinstance(syn[0][c], str) else "L", 16) for c in cols],
               [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in syn])
    return _xlsx_response(wb, f"commande_ops_{month or 'na'}.xlsx")


def _export_estimation(request):
    from openpyxl import Workbook

    from fuel_tracking.views import FuelCommandeEstimationView

    data = FuelCommandeEstimationView().get(request).data
    k = data.get("kpis") or {}
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Estimation commande", [
        ("Mois estimé", data.get("target_month") or "—"), ("Mois sources", ", ".join(data.get("source_months") or [])),
        ("Marge appliquée, %", str(data.get("marge_pct"))),
    ], [
        ("Sites estimés", k.get("nb_sites"), "int"), ("Commande estimée totale, L", k.get("total_commande_estimee_l"), "L"),
        ("Commande Ops de référence, L", k.get("total_commande_ops_reference_l"), "L"),
        ("Sites en rupture prévue", k.get("nb_sites_rupture_prevue"), "int"),
        ("Sites en confiance élevée", k.get("nb_sites_confiance_elevee"), "int"), ("Sites en confiance faible", k.get("nb_sites_confiance_faible"), "int"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Mois d'historique", "nb_mois_historique", "int", 10),
        ("Sources", lambda r: ", ".join(r.get("sources_historique") or []), "txt", 14),
        ("Conso / jour pondérée (L)", "conso_jour_ponderee_l", "dec1", 13), ("Conso projetée (L)", "conso_projetee_l", "L", 13),
        ("Stock actuel (L)", "stock_actuel_l", "L", 12), ("Capacité cuve (L)", "capacite_cuve_l", "L", 12),
        ("Commande sans marge (L)", "commande_sans_marge_l", "L", 14), ("Commande avec marge (L)", "commande_avec_marge_l", "L", 14),
        ("Plafonnée par capacité", lambda r: "Oui" if r.get("plafonnee_par_capacite") else "Non", "txt", 11),
        ("Stock final estimé (L)", "stock_final_estime_l", "L", 13), ("Confiance", "confiance", "txt", 10),
        ("Commande Ops de référence (L)", "commande_ops_reference_l", "L", 15),
    ], data.get("sites") or [])
    return _xlsx_response(wb, f"estimation_commande_{data.get('target_month') or 'na'}.xlsx")


class BiExportView(APIView):
    """GET /api/fuel-tracking/bi/export/<kind>/ — classeur Excel complet (toutes les lignes, pas seulement le Top 10)."""
    permission_classes = [IsAuthenticated]
    KINDS = ("conso", "stock", "cph", "commande", "estimation")

    def get(self, request, kind):
        p = request.query_params
        try:
            if kind == "conso":
                return _export_conso(p)
            if kind == "stock":
                return _export_stock(p)
            if kind == "cph":
                return _export_cph(p)
            if kind == "commande":
                return _export_commande(p)
            if kind == "estimation":
                return _export_estimation(request)
        except (_BadPeriod, ValueError) as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"detail": "Export inconnu.", "exports": self.KINDS}, status=404)
