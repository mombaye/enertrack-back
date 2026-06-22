# optimization/views.py

from datetime import date, datetime
from decimal import Decimal

from django.db.models import Q
from django.utils.dateparse import parse_date

from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from .models import OptimizationBatch, OptimizationResult
from .serializers import OptimizationBatchSerializer, OptimizationResultSerializer
from .services import (
    get_contracts_to_optimize,
    build_contract_annual_base,
    run_power_optimization_batch,
)


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


def _parse_bool(value, default=True):
    if value is None:
        return default

    value = str(value).strip().lower()

    if value in ("1", "true", "yes", "oui"):
        return True

    if value in ("0", "false", "no", "non"):
        return False

    return default


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, list):
        return [_json_safe(v) for v in value]

    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items() if k != "site"}

    return value


class OptimizationBatchViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = OptimizationBatch.objects.all().order_by("-launched_at")
    serializer_class = OptimizationBatchSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    @action(methods=["get"], detail=True, url_path="results")
    def results(self, request, pk=None):
        batch = self.get_object()

        qs = (
            batch.results
            .select_related("site")
            .all()
        )

        search = request.query_params.get("search")
        best_type = request.query_params.get("best_type")
        tariff = request.query_params.get("tariff")
        site_id = request.query_params.get("site_id")
        status = request.query_params.get("status")
        gain_only = request.query_params.get("gain_only")
        no_gain_only = request.query_params.get("no_gain_only")

        if search:
            qs = qs.filter(
                Q(numero_compte_contrat__icontains=search)
                | Q(site_code__icontains=search)
                | Q(site_name__icontains=search)
            )

        if best_type:
            qs = qs.filter(best_optimization_type=best_type.upper())

        if tariff:
            qs = qs.filter(tariff_current__iexact=tariff.strip())

        if site_id:
            qs = qs.filter(site_code__icontains=site_id.strip())

        if status:
            qs = qs.filter(status=status.upper())

        if _parse_bool(gain_only, default=False):
            qs = qs.filter(best_gain__gt=0)

        if _parse_bool(no_gain_only, default=False):
            qs = qs.filter(
                status=OptimizationResult.Status.OK
            ).filter(
                Q(best_gain__isnull=True) | Q(best_gain__lte=0)
            )

        qs = qs.order_by("-best_gain", "numero_compte_contrat")

        page = self.paginate_queryset(qs)
        if page is not None:
            serializer = OptimizationResultSerializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = OptimizationResultSerializer(qs, many=True)
        return Response(serializer.data)


class OptimizationResultViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = (
        OptimizationResult.objects
        .select_related("batch", "site")
        .all()
    )
    serializer_class = OptimizationResultSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        qs = super().get_queryset()

        batch_id = self.request.query_params.get("batch")
        search = self.request.query_params.get("search")
        best_type = self.request.query_params.get("best_type")
        site_id = self.request.query_params.get("site_id")
        tariff_current = self.request.query_params.get("tariff_current")
        tariff_optimized = self.request.query_params.get("tariff_optimized")
        status = self.request.query_params.get("status")
        gain_only = self.request.query_params.get("gain_only")
        no_gain_only = self.request.query_params.get("no_gain_only")

        if batch_id:
            qs = qs.filter(batch_id=batch_id)

        if search:
            qs = qs.filter(
                Q(numero_compte_contrat__icontains=search)
                | Q(site_code__icontains=search)
                | Q(site_name__icontains=search)
            )

        if best_type:
            qs = qs.filter(best_optimization_type=best_type.upper())

        if site_id:
            qs = qs.filter(site_code__icontains=site_id.strip())

        if tariff_current:
            qs = qs.filter(tariff_current__iexact=tariff_current.strip())

        if tariff_optimized:
            qs = qs.filter(tariff_optimized__iexact=tariff_optimized.strip())

        if status:
            qs = qs.filter(status=status.upper())

        if _parse_bool(gain_only, default=False):
            qs = qs.filter(best_gain__gt=0)

        if _parse_bool(no_gain_only, default=False):
            qs = qs.filter(
                status=OptimizationResult.Status.OK
            ).filter(
                Q(best_gain__isnull=True) | Q(best_gain__lte=0)
            )

        return qs.order_by("-best_gain", "numero_compte_contrat")


class OptimizationContractsAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        only_eligible = _parse_bool(request.query_params.get("eligible"), default=True)
        search = request.query_params.get("search")

        try:
            page = int(request.query_params.get("page", 1))
        except Exception:
            page = 1

        try:
            limit = int(request.query_params.get("limit", 100))
        except Exception:
            limit = 100

        page = max(page, 1)
        limit = max(1, min(limit, 500))

        contracts = get_contracts_to_optimize(only_eligible_sites=only_eligible)

        if search:
            s = search.strip().lower()
            contracts = [c for c in contracts if s in str(c).lower()]

        total = len(contracts)
        start = (page - 1) * limit
        end = start + limit

        return Response({
            "eligible_only": only_eligible,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": (total + limit - 1) // limit if total else 1,
            },
            "data": contracts[start:end],
        })


class OptimizationContractAnnualBaseAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, numero_compte_contrat):
        only_eligible = _parse_bool(request.query_params.get("eligible"), default=True)

        ref_date_raw = request.query_params.get("ref_date")
        ref_date = parse_date(ref_date_raw) if ref_date_raw else None

        base = build_contract_annual_base(
            numero_compte_contrat=numero_compte_contrat,
            only_eligible_sites=only_eligible,
            reference_date=ref_date,
        )

        return Response(_json_safe(base))


class RunPowerOptimizationAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        only_eligible = _parse_bool(request.data.get("eligible"), default=True)

        ref_date_raw = request.data.get("ref_date")
        ref_date = parse_date(ref_date_raw) if ref_date_raw else None

        batch = run_power_optimization_batch(
            user=request.user,
            only_eligible_sites=only_eligible,
            reference_date=ref_date,
        )

        return Response({
            "message": "Optimisation puissance & tarif lancée avec succès.",
            "batch": OptimizationBatchSerializer(batch).data,
        })