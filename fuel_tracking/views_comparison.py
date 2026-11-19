# fuel_tracking/views_comparison.py
"""
« Suivis Consommations » — tableau unique, période unique.

  GET /api/fuel-tracking/consommations/                 KPI + tableau (pagination serveur) + facettes + période par défaut
  GET /api/fuel-tracking/consommations/periodes/        périodes disposant d'un bilan (+ défaut)
  GET /api/fuel-tracking/consommations/export/<kind>/   controle_complet | anomalies_fuel | bilan_certification (CSV)
Paramètres : date_debut, date_fin (AAAA-MM-JJ, jours entiers 00:00:00 → 23:59:59), filtres pays, zone, site, typologie, configuration,
typologie_energie, statut_bilan, statut_certification, source_stock, source_runtime, source_puissance, source_target, qualite (reserve|sans_reserve|anomalie), tri, page, page_size.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import available_periods, default_period, to_day


def _base_ge_reference(site_ids):
    """Retourne les libellés site → fabricant/type du dernier lot Base GE importé.

    La lecture des lignes brutes du lot est la source de vérité : contrairement
    aux valeurs mensuelles, elle couvre aussi un site qui n'avait pas encore de
    FuelConsommationMonthly lors du dépôt du fichier.
    """
    from fuel_tracking.models import FuelImportBatch, FuelImportRow

    batch = (FuelImportBatch.objects.filter(source_type="BASE_GE", status__in=("ACCEPTED", "VALIDATION_REQUIRED"))
             .order_by("-received_at", "-id").first())
    if batch is None or not site_ids:
        return {}

    by_site = {}
    rows = FuelImportRow.objects.filter(batch=batch, site_id_resolved__in=site_ids).order_by("row_number", "id")
    for row in rows:
        payload = row.raw_payload or {}
        site_id = row.site_id_resolved
        ge_type = str(payload.get("type_ge") or "").strip()
        manufacturer = str(payload.get("fabricant_ge") or "").strip()
        if ge_type:
            # Le dernier doublon dans le fichier fait foi.
            by_site[site_id] = {"type_ge": ge_type, "fabricant_ge": manufacturer or _manufacturer_from_label(ge_type),
                                "source_ge": "Base GE"}
    return by_site


def _manufacturer_from_label(label: str) -> str:
    """Extract common manufacturer names when Base GE only supplies a combined GE label."""
    value = " ".join(label.split())
    upper = value.upper()
    known = (
        "JOHN DEERE", "FG WILSON", "CATERPILLAR", "MITSUBISHI", "LISTER PETTER",
        "HIMOINSA", "BAUDOUIN", "CUMMINS", "PERKINS", "KOHLER", "GENERAC", "DOOSAN",
        "YANMAR", "VOLVO", "DENYO", "SDMO", "JCB", "MTU", "CAT",
    )
    for manufacturer in known:
        if upper == manufacturer or upper.startswith(manufacturer + " ") or upper.startswith(manufacturer + "-"):
            return manufacturer
    # N'invente pas de marque à partir d'un modèle non reconnu : afficher le
    # libellé complet garantit que l'information Base GE reste visible.
    return value


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

        # Même référence Base GE pour chaque période sélectionnée. L'import est
        # un référentiel courant, pas une valeur mensuelle : il prime pour le
        # Type de GE affiché, quel que soit le mois du bilan consulté.
        base_ge = _base_ge_reference({r["site_id"] for r in rows})
        for row in rows:
            ref = base_ge.get(row["site_id"])
            cph_label = (row.get("modele_ge") or "").strip()
            if ref:
                row["estimation_theorique"] = {
                    "fabricant_ge": ref["fabricant_ge"],
                    "type_ge": ref["type_ge"],
                    "source_ge": ref["source_ge"],
                }
            elif cph_label:
                row["estimation_theorique"] = {
                    "fabricant_ge": _manufacturer_from_label(cph_label),
                    "type_ge": cph_label,
                    "source_ge": "Inventaire CPH (Snowflake / ENOC)",
                }
            else:
                row["estimation_theorique"] = {"fabricant_ge": None, "type_ge": None, "source_ge": None}

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


class ConsommationsSanteView(APIView):
    """GET /api/fuel-tracking/consommations/sante/ — fraîcheur des sources, couverture des relevés gardiens, sites en retard."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services.data_health import health

        return Response(health())


class ConsommationsEvolutionView(APIView):
    """GET /api/fuel-tracking/consommations/evolution/ — totaux par période (variation de stock, Closing) pour les graphiques."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(C.evolution(12))
