# fuel_tracking/views_comparison.py
"""
« Suivis Consommations » — tableau unique, période unique.

  GET /api/fuel-tracking/consommations/                 KPI + tableau (pagination serveur) + facettes + période par défaut
  GET /api/fuel-tracking/consommations/periodes/        périodes disposant d'un bilan (+ défaut)
  GET /api/fuel-tracking/consommations/export/<kind>/   controle_complet | anomalies_fuel | bilan_certification (CSV)
Paramètres : date_debut, date_fin (AAAA-MM-JJ, jours entiers 00:00:00 → 23:59:59), filtres pays, zone, site, typologie, configuration,
statut_global, qualite_reel, source_runtime, source_puissance, statut_cph, tri, page, page_size.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import available_periods, default_period, to_day


def _period(p):
    d_from, d_to = to_day(p.get("date_debut") or p.get("start")), to_day(p.get("date_fin") or p.get("end"))
    if d_from and d_to:
        if d_to < d_from:
            raise ValueError("date_fin est antérieure à date_debut.")
        return d_from, d_to, False
    dflt = default_period()
    if dflt is None:
        return None, None, True
    return dflt[0], dflt[1], True


class ConsommationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        try:
            d_from, d_to, is_default = _period(p)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if d_from is None:
            return Response({"detail": "Aucun bilan de stock calculé : lancez le calcul des bilans.", "rows": [], "total": 0, "periode": None}, status=200)
        data = C.build(d_from, d_to, p)
        rows = data.pop("rows")
        try:
            size = max(1, min(int(p.get("page_size", 50)), 200))
            page = max(1, int(p.get("page", 1)))
        except ValueError:
            size, page = 50, 1
        data["total"] = len(rows)
        data["page"], data["page_size"] = page, size
        data["rows"] = rows[(page - 1) * size: page * size]
        data["periode"]["par_defaut"] = is_default
        return Response(data)


class ConsommationsPeriodesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        dflt = default_period()
        return Response({"periodes": available_periods(50), "defaut": {"debut": dflt[0].isoformat(), "fin": dflt[1].isoformat()} if dflt else None})


class ConsommationsExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, kind):
        if kind not in C.EXPORTS:
            return Response({"detail": "Export inconnu.", "exports": C.EXPORTS}, status=404)
        try:
            d_from, d_to, _ = _period(request.query_params)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if d_from is None:
            return Response({"detail": "Aucun bilan de stock calculé."}, status=404)
        name, content, n = C.export_csv(kind, d_from, d_to, request.query_params)
        r = HttpResponse(content, content_type="text/csv; charset=utf-8")
        r["Content-Disposition"] = f'attachment; filename="{name}"'
        r["X-Row-Count"] = str(n)
        return r
