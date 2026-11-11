"""
Vues « BI » du Suivi carburant (graphiques, KPI, Top 10) — lecture seule, agrégats déjà en base.

  GET /api/fuel-tracking/bi/conso/?from_month=YYYY-MM&to_month=YYYY-MM&country=&typology=
  GET /api/fuel-tracking/bi/stock/?country=&month=YYYY-MM
  GET /api/fuel-tracking/bi/cph/?start=YYYY-MM-DD&end=YYYY-MM-DD&country=

Règles (CLAUDE.md) : périmètre « avec GE » identique aux autres écrans ; mesuré et estimé jamais
additionnés ; une valeur absente reste null (jamais 0) ; ratios = ratio des sommes, pas moyenne de ratios.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

TOP_N = 10
# En dessous, le ratio L/kWh d'un site est trop bruité pour un classement.
MIN_KWH_FOR_RATIO = 100


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


# ─── Consommation ────────────────────────────────────────────────────────────

class BiConsoView(APIView):
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _totals(qs):
        both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
        a = qs.aggregate(
            nb_sites=Count("site_id", distinct=True),
            nb_sites_mesure=Count("site_id", distinct=True, filter=Q(conso_snowflake_l__isnull=False)),
            conso_mesuree_l=Sum("conso_snowflake_l"),
            enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
            nb_ravitaillements=Sum("enoc_nb_demandes"),
            conso_estimee_enoc_l=Sum("conso_estimee_enoc_l"),
            num=Sum("conso_snowflake_l", filter=both),
            den=Sum("ge_prod_kwh", filter=both),
            ge_prod_kwh=Sum("ge_prod_kwh"),
        )
        return {
            "nb_sites": a["nb_sites"],
            "nb_sites_mesure": a["nb_sites_mesure"],
            "couverture_pct": round(a["nb_sites_mesure"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
            "conso_mesuree_l": _f(a["conso_mesuree_l"]),
            "enoc_ajoutee_l": _f(a["enoc_ajoutee_l"]),
            "nb_ravitaillements": a["nb_ravitaillements"],
            "conso_estimee_enoc_l": _f(a["conso_estimee_enoc_l"]),
            "ge_prod_kwh": _f(a["ge_prod_kwh"]),
            "l_kwh": _ratio(a["num"], a["den"]),
        }

    @staticmethod
    def _breakdown(qs, field):
        both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
        rows = (
            qs.values(key=F(field))
            .annotate(
                nb_sites=Count("site_id", distinct=True),
                conso_mesuree_l=Sum("conso_snowflake_l"),
                enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
                num=Sum("conso_snowflake_l", filter=both),
                den=Sum("ge_prod_kwh", filter=both),
            )
            .order_by(F("conso_mesuree_l").desc(nulls_last=True))
        )
        return [{
            "label": r["key"] or "Non renseigné", "nb_sites": r["nb_sites"],
            "conso_mesuree_l": _f(r["conso_mesuree_l"]), "enoc_ajoutee_l": _f(r["enoc_ajoutee_l"]),
            "l_kwh": _ratio(r["num"], r["den"]),
        } for r in rows]

    def get(self, request):
        from fuel_tracking.models import FuelConsommationMonthly
        from fuel_tracking.views import _effective_ge_q, _file_ge_site_ids

        p = request.query_params
        base = FuelConsommationMonthly.objects.filter(_effective_ge_q(_file_ge_site_ids())).annotate(
            typo=Coalesce("typology_fichier", "typology", Value(None)))
        all_months = list(base.order_by("month_year").values_list("month_year", flat=True).distinct())
        countries = sorted({c for c in base.values_list("country", flat=True).distinct() if c})
        typologies = sorted({t for t in base.values_list("typo", flat=True).distinct() if t})
        if not all_months:
            return Response({"months": [], "available_months": [], "filters": {"countries": [], "typologies": []}})

        to_month = (p.get("to_month") or "").strip() or all_months[-1]
        from_month = (p.get("from_month") or "").strip() or _shift_month(to_month, -5)
        if from_month > to_month:
            from_month, to_month = to_month, from_month
        if p.get("country"):
            base = base.filter(country__iexact=p["country"])
        if p.get("typology"):
            base = base.filter(typo=p["typology"])

        months = [m for m in all_months if from_month <= m <= to_month]
        span = len(months) or 1
        period = base.filter(month_year__in=months)
        # Période précédente de même longueur, pour les variations des KPI.
        prev_months = [m for m in all_months if _shift_month(from_month, -span) <= m < from_month]
        prev = base.filter(month_year__in=prev_months)

        monthly = []
        for m in months:
            t = self._totals(period.filter(month_year=m))
            monthly.append({"month": m, **t})

        cur_t, prev_t = self._totals(period), self._totals(prev) if prev_months else None
        deltas = {k: _pct_change(cur_t[k], prev_t[k]) if prev_t else None
                  for k in ("conso_mesuree_l", "enoc_ajoutee_l", "l_kwh")}
        if prev_t and cur_t["couverture_pct"] is not None and prev_t["couverture_pct"] is not None:
            deltas["couverture_pts"] = round(cur_t["couverture_pct"] - prev_t["couverture_pct"], 1)

        per_site = list(
            period.values("site_id")
            .annotate(
                site_name=Coalesce(F("site_name"), Value("")),
                conso=Sum("conso_snowflake_l"), enoc=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
                num=Sum("conso_snowflake_l", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
                den=Sum("ge_prod_kwh", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
                nb_mois=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
            )
        )
        names = dict(period.exclude(site_name__isnull=True).values_list("site_id", "site_name"))
        country_of = dict(period.exclude(country__isnull=True).values_list("site_id", "country"))

        def site(r, **extra):
            return {"site_id": r["site_id"], "site_name": names.get(r["site_id"]), "country": country_of.get(r["site_id"]), **extra}

        top_conso = sorted((r for r in per_site if r["conso"] is not None), key=lambda r: -r["conso"])[:TOP_N]
        gaps = [r for r in per_site if r["conso"] is not None and r["enoc"] is not None]
        top_gap = sorted(gaps, key=lambda r: -abs(float(r["conso"]) - float(r["enoc"])))[:TOP_N]
        ratios = [r for r in per_site if r["den"] is not None and float(r["den"]) >= MIN_KWH_FOR_RATIO and r["num"] is not None]
        top_ratio = sorted(ratios, key=lambda r: -float(r["num"]) / float(r["den"]))[:TOP_N]

        buckets = [("< 0,20", None, 0.20), ("0,20–0,25", 0.20, 0.25), ("0,25–0,30", 0.25, 0.30),
                   ("0,30–0,35", 0.30, 0.35), ("0,35–0,45", 0.35, 0.45), ("> 0,45", 0.45, None)]
        dist = []
        for label, lo, hi in buckets:
            n = sum(1 for r in ratios if (lo is None or float(r["num"]) / float(r["den"]) >= lo)
                    and (hi is None or float(r["num"]) / float(r["den"]) < hi))
            dist.append({"label": label, "nb_sites": n, "reference": lo == 0.25})

        return Response({
            "from_month": months[0] if months else from_month, "to_month": months[-1] if months else to_month,
            "months": months, "available_months": all_months,
            "previous_months": prev_months,
            "filters": {"countries": countries, "typologies": typologies},
            "kpis": cur_t, "deltas": deltas,
            "monthly": monthly,
            "by_country": self._breakdown(period, "country"),
            "by_typology": self._breakdown(period, "typo"),
            "top_conso": [site(r, conso_mesuree_l=_f(r["conso"]), nb_mois=r["nb_mois"]) for r in top_conso],
            "top_ecart_enoc": [site(r, conso_mesuree_l=_f(r["conso"]), enoc_ajoutee_l=_f(r["enoc"]),
                                    ecart_l=_f(float(r["conso"]) - float(r["enoc"])),
                                    ecart_pct=_pct_change(r["conso"], r["enoc"])) for r in top_gap],
            "top_l_kwh": [site(r, l_kwh=_ratio(r["num"], r["den"]), conso_mesuree_l=_f(r["num"]), ge_prod_kwh=_f(r["den"]))
                          for r in top_ratio],
            "distribution_l_kwh": dist,
            "reference_l_kwh": [0.25, 0.30],
        })


# ─── Stock ───────────────────────────────────────────────────────────────────

class BiStockView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot

        p = request.query_params
        qs = FuelStockSnapshot.objects.filter(snapshot_year__isnull=True, has_genset=True)
        countries = sorted({c for c in qs.values_list("country", flat=True).distinct() if c})
        if p.get("country"):
            qs = qs.filter(country__iexact=p["country"])

        known = Q(stock_snowflake_pct__isnull=False)
        a = qs.aggregate(
            nb_sites=Count("id"),
            nb_stock_connu=Count("id", filter=Q(stock_snowflake_l__isnull=False) | Q(stock_enoc_l__isnull=False)),
            stock_l=Sum("stock_snowflake_l"), capacite_l=Sum("capacity_snowflake_l", filter=Q(stock_snowflake_l__isnull=False)),
            critique=Count("id", filter=Q(stock_snowflake_pct__lt=15)),
            alerte=Count("id", filter=Q(stock_snowflake_pct__gte=15, stock_snowflake_pct__lt=40)),
            moyen=Count("id", filter=Q(stock_snowflake_pct__gte=40, stock_snowflake_pct__lt=70)),
            bon=Count("id", filter=Q(stock_snowflake_pct__gte=70)),
            sans_pct=Count("id", filter=~known),
        )
        last_date = qs.exclude(stock_snowflake_date__isnull=True).order_by("-stock_snowflake_date") \
            .values_list("stock_snowflake_date", flat=True).first()

        # Autonomie = stock actuel / conso mesurée moyenne par jour du dernier mois complet disponible.
        months = list(FuelConsommationMonthly.objects.order_by("-month_year").values_list("month_year", flat=True).distinct()[:2])
        ref_month = (p.get("month") or "").strip() or (months[1] if len(months) > 1 else (months[0] if months else None))
        daily = {}
        if ref_month:
            y, m = int(ref_month[:4]), int(ref_month[5:7])
            import calendar
            ndays = calendar.monthrange(y, m)[1]
            daily = {sid: float(c) / ndays for sid, c in FuelConsommationMonthly.objects
                     .filter(month_year=ref_month, conso_snowflake_l__gt=0).values_list("site_id", "conso_snowflake_l")}

        rows = list(qs.values("site_id", "site_name", "country", "stock_snowflake_l", "capacity_snowflake_l",
                              "stock_snowflake_pct", "stock_snowflake_date", "stock_enoc_l"))
        for r in rows:
            d = daily.get(r["site_id"])
            r["autonomie_j"] = round(float(r["stock_snowflake_l"]) / d, 1) if d and r["stock_snowflake_l"] is not None else None

        def out(r):
            return {"site_id": r["site_id"], "site_name": r["site_name"], "country": r["country"],
                    "stock_l": _f(r["stock_snowflake_l"]), "capacite_l": _f(r["capacity_snowflake_l"]),
                    "remplissage_pct": _f(r["stock_snowflake_pct"]), "date": r["stock_snowflake_date"],
                    "stock_enoc_l": _f(r["stock_enoc_l"]), "autonomie_j": r["autonomie_j"]}

        with_pct = [r for r in rows if r["stock_snowflake_pct"] is not None]
        with_auto = [r for r in rows if r["autonomie_j"] is not None]
        auto_buckets = [("< 3 j", 0, 3), ("3–7 j", 3, 7), ("7–15 j", 7, 15), ("15–30 j", 15, 30), ("> 30 j", 30, None)]

        by_country = (qs.values(key=F("country")).annotate(
            nb_sites=Count("id"), stock_l=Sum("stock_snowflake_l"),
            capacite_l=Sum("capacity_snowflake_l", filter=Q(stock_snowflake_l__isnull=False)),
            critique=Count("id", filter=Q(stock_snowflake_pct__lt=15))).order_by("-nb_sites"))

        return Response({
            "filters": {"countries": countries},
            "kpis": {
                "nb_sites": a["nb_sites"], "nb_stock_connu": a["nb_stock_connu"],
                "stock_connu_pct": round(a["nb_stock_connu"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
                "stock_total_l": _f(a["stock_l"]), "capacite_l": _f(a["capacite_l"]),
                "remplissage_pct": round(float(a["stock_l"]) / float(a["capacite_l"]) * 100, 1) if a["stock_l"] is not None and a["capacite_l"] else None,
                "nb_critique": a["critique"], "nb_alerte": a["alerte"],
                "nb_autonomie_moins_3j": sum(1 for r in with_auto if r["autonomie_j"] < 3),
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
                {"label": label, "nb_sites": sum(1 for r in with_auto if r["autonomie_j"] >= lo and (hi is None or r["autonomie_j"] < hi))}
                for label, lo, hi in auto_buckets
            ],
            "by_country": [{"label": r["key"] or "Non renseigné", "nb_sites": r["nb_sites"], "stock_l": _f(r["stock_l"]),
                            "remplissage_pct": round(float(r["stock_l"]) / float(r["capacite_l"]) * 100, 1) if r["stock_l"] is not None and r["capacite_l"] else None,
                            "nb_critique": r["critique"]} for r in by_country],
            "top_critique": [out(r) for r in sorted(with_pct, key=lambda r: float(r["stock_snowflake_pct"]))[:TOP_N]],
            "top_autonomie_faible": [out(r) for r in sorted(with_auto, key=lambda r: r["autonomie_j"])[:TOP_N]],
            "top_stock": [out(r) for r in sorted((r for r in rows if r["stock_snowflake_l"] is not None),
                                                   key=lambda r: -float(r["stock_snowflake_l"]))[:TOP_N]],
        })


# ─── CPH ─────────────────────────────────────────────────────────────────────

class BiCphView(APIView):
    """Vue BI sur le calcul CPH de la période (même calcul mis en cache que l'onglet Contrôle CPH)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services.cph_service import compute_period_summary
        from fuel_tracking.views_cph import _parse_period, _periode_info

        try:
            start, end = _parse_period(request.query_params)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if end < start:
            start, end = end, start
        if (end - start).days > 92:
            return Response({"detail": "Période CPH limitée à 93 jours."}, status=400)
        try:
            rows = compute_period_summary(start, end, country=request.query_params.get("country") or None)
        except Exception as e:  # source indisponible : l'écran affiche « indisponible », jamais 0
            return Response({"detail": f"Calcul CPH indisponible ({type(e).__name__})."}, status=503)

        ge = [r for r in rows if r.get("perimetre") == "GE"]
        meas = lambda r: (r.get("comparaison") or {}).get("conso_mesuree_l")  # noqa: E731
        theo = [r for r in ge if r.get("conso_theorique_l") is not None]
        rt = [r for r in ge if r.get("runtime_total_h") is not None]
        both = [r for r in theo if meas(r) is not None]

        status_counts = {}
        for r in ge:
            status_counts[r.get("cph_status") or "NON_CALCULE"] = status_counts.get(r.get("cph_status") or "NON_CALCULE", 0) + 1
        motifs = {}
        for r in ge:
            if r.get("cph_status") != "COMPLET":
                code = ((r.get("motif_cph") or {}).get("code")) or "AUTRE"
                motifs[code] = motifs.get(code, 0) + 1

        by_country = {}
        for r in ge:
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

        def site(r, **extra):
            return {"site_id": r["site_id"], "site_name": r.get("site_name"), "country": r.get("country"), **extra}

        sum_theo = sum(float(r["conso_theorique_l"]) for r in both)
        sum_meas = sum(float(meas(r)) for r in both)
        charge_pts = [r for r in ge if r.get("charge_moy") is not None and r.get("cph_moy_l_h") is not None][:800]

        return Response({
            "start": start, "end": end, "periode": _periode_info(start, end),
            "kpis": {
                "nb_sites_ge": len(ge), "nb_cph_complet": len(theo),
                "couverture_cph_pct": round(len(theo) / len(ge) * 100, 1) if ge else None,
                "conso_theorique_l": round(sum(float(r["conso_theorique_l"]) for r in theo), 1) if theo else None,
                "runtime_total_h": round(sum(float(r["runtime_total_h"]) for r in rt), 1) if rt else None,
                "cph_moy_l_h": _ratio(sum(float(r["conso_theorique_l"]) for r in theo),
                                      sum(float(r["runtime_total_h"]) for r in theo if r.get("runtime_total_h") is not None) or None, 2) if theo else None,
                # Comparaison uniquement sur les sites ayant les DEUX valeurs (jamais d'addition mesuré + estimé).
                "nb_sites_comparables": len(both),
                "conso_theorique_comparable_l": round(sum_theo, 1) if both else None,
                "conso_mesuree_comparable_l": round(sum_meas, 1) if both else None,
                "ecart_global_pct": _pct_change(sum_meas, sum_theo) if both else None,
            },
            "statuts_cph": [{"label": k, "nb_sites": v} for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])],
            "motifs_non_calcule": [{"label": k, "nb_sites": v} for k, v in sorted(motifs.items(), key=lambda kv: -kv[1])[:8]],
            "by_country": sorted(by_country.values(), key=lambda b: -b["nb_sites"]),
            "charge_vs_cph": [{"site_id": r["site_id"], "charge_pct": round(float(r["charge_moy"]) * 100, 1),
                               "cph_l_h": round(float(r["cph_moy_l_h"]), 2)} for r in charge_pts],
            "top_conso_theorique": [site(r, conso_theorique_l=_f(r["conso_theorique_l"]), runtime_h=_f(r.get("runtime_total_h")),
                                         cph_l_h=_f(r.get("cph_moy_l_h"), 2))
                                    for r in sorted(theo, key=lambda r: -float(r["conso_theorique_l"]))[:TOP_N]],
            "top_ecart_mesure": [site(r, conso_theorique_l=_f(r["conso_theorique_l"]), conso_mesuree_l=_f(meas(r)),
                                      ecart_l=_f(float(meas(r)) - float(r["conso_theorique_l"])),
                                      ecart_pct=_pct_change(meas(r), r["conso_theorique_l"]))
                                 for r in sorted(both, key=lambda r: -abs(float(meas(r)) - float(r["conso_theorique_l"])))[:TOP_N]],
            "top_runtime": [site(r, runtime_h=_f(r["runtime_total_h"]), charge_pct=_f(float(r["charge_moy"]) * 100) if r.get("charge_moy") is not None else None)
                            for r in sorted(rt, key=lambda r: -float(r["runtime_total_h"]))[:TOP_N]],
        })
