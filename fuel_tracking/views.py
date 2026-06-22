from django.shortcuts import render

# Create your views here.
from decimal import Decimal

from django.db.models import Sum, Count, Q
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


# fuel_tracking/views.py

from datetime import datetime

from math import ceil

from django.db.models import Q
from django.utils.dateparse import parse_date


from fuel_tracking.models import (
    FuelEfmsMonthly,
    FuelEfmsSyncRun,
    FuelEnocMovement,
    FuelEnocSyncRun, 
    FuelEfmsSyncRun
)
from fuel_tracking.serializers import (
    FuelEnocMovementSerializer,
    FuelEnocSyncRunSerializer,
    FuelEfmsSyncRunSerializer,
    FuelEfmsMonthlySerializer
)


def _to_float(value):
    if value is None:
        return 0
    try:
        return float(value)
    except Exception:
        return 0


def _paginate_queryset(qs, request):
    page = int(request.query_params.get("page", 1))
    limit = int(request.query_params.get("limit", 50))

    if page < 1:
        page = 1

    if limit < 10:
        limit = 10

    if limit > 200:
        limit = 200

    total = qs.count()
    start = (page - 1) * limit
    end = start + limit

    return qs[start:end], {
        "page": page,
        "limit": limit,
        "total": total,
        "totalPages": (total + limit - 1) // limit if total else 1,
        "hasNext": end < total,
        "hasPrev": page > 1,
    }


def _base_queryset(request):
    country = request.query_params.get("country", "Senegal")
    year = request.query_params.get("year")
    month = request.query_params.get("month")
    month_year = request.query_params.get("month_year")
    site = request.query_params.get("site")
    anomaly = request.query_params.get("anomaly")
    only_active = request.query_params.get("only_active")

    qs = FuelEfmsMonthly.objects.filter(country__iexact=country)

    if month_year:
        qs = qs.filter(month_year=month_year)
    else:
        if year:
            qs = qs.filter(year=int(year))
        if month:
            qs = qs.filter(month=int(month))

    if site:
        qs = qs.filter(
            Q(site_id__icontains=site)
            | Q(site_name__icontains=site)
        )

    if anomaly:
        qs = qs.filter(anomaly_flags__contains=[anomaly])

    if only_active in {"1", "true", "True", "yes"}:
        qs = qs.filter(
            Q(fuel_order_l__gt=0)
            | Q(fuel_deli_l__gt=0)
            | Q(fuel_conso_l__gt=0)
            | Q(ge_working_hours__gt=0)
        )

    return qs


class FuelEfmsMonthlyListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _base_queryset(request).order_by("-year", "-month", "site_id")

        paginated, pagination = _paginate_queryset(qs, request)

        return Response({
            "data": FuelEfmsMonthlySerializer(paginated, many=True).data,
            "pagination": pagination,
        })


class FuelEfmsDashboardView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = _base_queryset(request)

        agg = qs.aggregate(
            total_rows=Count("id"),
            total_order_l=Sum("fuel_order_l"),
            total_deli_l=Sum("fuel_deli_l"),
            total_conso_l=Sum("fuel_conso_l"),
            total_ge_hours=Sum("ge_working_hours"),
            total_abnormal_ge_hours=Sum("abnormal_ge_working_hours"),
            total_monitoring_unavailability_hours=Sum("monitoring_unavailability_hours"),
        )

        total_conso = agg["total_conso_l"] or Decimal("0")
        total_ge_hours = agg["total_ge_hours"] or Decimal("0")

        cph_global = None
        if total_ge_hours > 0 and total_conso > 0:
            cph_global = total_conso / total_ge_hours

        active_sites = qs.filter(
            Q(fuel_order_l__gt=0)
            | Q(fuel_deli_l__gt=0)
            | Q(fuel_conso_l__gt=0)
            | Q(ge_working_hours__gt=0)
        ).count()

        sites_with_conso = qs.filter(fuel_conso_l__gt=0).count()
        sites_with_ge_hours = qs.filter(ge_working_hours__gt=0).count()

        anomaly_codes = [
            "DELIVERY_GT_ORDER",
            "CONSO_WITHOUT_GE_HOURS",
            "GE_HOURS_WITHOUT_CONSO",
            "ABNORMAL_GE_HOURS",
            "HIGH_MONITORING_UNAVAILABILITY",
        ]

        anomalies = {}
        for code in anomaly_codes:
            anomalies[code] = qs.filter(anomaly_flags__contains=[code]).count()

        top_conso = qs.filter(fuel_conso_l__gt=0).order_by("-fuel_conso_l")[:10]
        top_cph = qs.filter(cph_l_per_hour__isnull=False).order_by("-cph_l_per_hour")[:10]
        top_ge_hours = qs.filter(ge_working_hours__gt=0).order_by("-ge_working_hours")[:10]

        monthly_evolution = (
            FuelEfmsMonthly.objects
            .filter(country__iexact=request.query_params.get("country", "Senegal"))
            .values("month_year", "year", "month")
            .annotate(
                fuel_order_l=Sum("fuel_order_l"),
                fuel_deli_l=Sum("fuel_deli_l"),
                fuel_conso_l=Sum("fuel_conso_l"),
                ge_working_hours=Sum("ge_working_hours"),
            )
            .order_by("year", "month")
        )

        return Response({
            "filters": {
                "country": request.query_params.get("country", "Senegal"),
                "year": request.query_params.get("year"),
                "month": request.query_params.get("month"),
                "month_year": request.query_params.get("month_year"),
                "site": request.query_params.get("site"),
                "anomaly": request.query_params.get("anomaly"),
            },
            "kpis": {
                "total_rows": agg["total_rows"] or 0,
                "active_sites": active_sites,
                "sites_with_conso": sites_with_conso,
                "sites_with_ge_hours": sites_with_ge_hours,
                "total_order_l": _to_float(agg["total_order_l"]),
                "total_deli_l": _to_float(agg["total_deli_l"]),
                "total_conso_l": _to_float(total_conso),
                "total_ge_hours": _to_float(total_ge_hours),
                "total_abnormal_ge_hours": _to_float(agg["total_abnormal_ge_hours"]),
                "total_monitoring_unavailability_hours": _to_float(
                    agg["total_monitoring_unavailability_hours"]
                ),
                "cph_global": _to_float(cph_global) if cph_global is not None else None,
            },
            "anomalies": anomalies,
            "top_conso": FuelEfmsMonthlySerializer(top_conso, many=True).data,
            "top_cph": FuelEfmsMonthlySerializer(top_cph, many=True).data,
            "top_ge_hours": FuelEfmsMonthlySerializer(top_ge_hours, many=True).data,
            "monthly_evolution": [
                {
                    "month_year": row["month_year"],
                    "year": row["year"],
                    "month": row["month"],
                    "fuel_order_l": _to_float(row["fuel_order_l"]),
                    "fuel_deli_l": _to_float(row["fuel_deli_l"]),
                    "fuel_conso_l": _to_float(row["fuel_conso_l"]),
                    "ge_working_hours": _to_float(row["ge_working_hours"]),
                    "cph_global": (
                        _to_float(row["fuel_conso_l"] / row["ge_working_hours"])
                        if row["ge_working_hours"] and row["fuel_conso_l"]
                        else None
                    ),
                }
                for row in monthly_evolution
            ],
        })





def _f(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def _month_bounds(month_year: str):
    """
    month_year attendu : YYYY-MM
    """
    try:
        start = parse_date(f"{month_year}-01")
        if not start:
            raise ValueError
    except Exception:
        now = datetime.utcnow()
        start = parse_date(f"{now.year}-{now.month:02d}-01")
        month_year = f"{now.year}-{now.month:02d}"

    year = start.year
    month = start.month

    if month == 12:
        end = parse_date(f"{year + 1}-01-01")
    else:
        end = parse_date(f"{year}-{month + 1:02d}-01")

    return month_year, start, end


def _gap(value_a, value_b):
    """
    value_a - value_b avec % basé sur value_a.
    Ici :
    - value_a peut être eFMS livré ou conso eFMS
    - value_b peut être ENOC réel
    """
    a = _f(value_a)
    b = _f(value_b)

    diff = b - a

    if a <= 0 and b <= 0:
        return None, None

    if a <= 0 and b > 0:
        return diff, 100.0

    pct = (diff / a) * 100
    return round(diff, 3), round(pct, 2)


def _row_status(has_efms: bool, has_enoc: bool, deli_gap_pct):
    if has_efms and has_enoc:
        if deli_gap_pct is None:
            return {
                "code": "NO_BASE",
                "label": "Base insuffisante",
                "tone": "slate",
            }

        abs_pct = abs(deli_gap_pct)

        if abs_pct <= 5:
            return {
                "code": "OK",
                "label": "Rapproché",
                "tone": "green",
            }

        if abs_pct <= 15:
            return {
                "code": "WARNING",
                "label": "Écart à suivre",
                "tone": "orange",
            }

        return {
            "code": "NOK",
            "label": "Écart important",
            "tone": "red",
        }

    if has_efms and not has_enoc:
        return {
            "code": "EFMS_ONLY",
            "label": "eFMS seul",
            "tone": "blue",
        }

    if has_enoc and not has_efms:
        return {
            "code": "ENOC_ONLY",
            "label": "ENOC seul",
            "tone": "violet",
        }

    return {
        "code": "NO_DATA",
        "label": "Aucune donnée",
        "tone": "slate",
    }


class FuelMonthlyTrackingView(APIView):
    """
    Suivi mensuel carburant :
    - eFMS : commandé, livré, consommé, heures GE
    - ENOC : opérations terrain, quantité réelle
    - Rapprochement simple eFMS vs ENOC
    """

    def get(self, request):
        month = request.query_params.get("month")
        country = request.query_params.get("country", "Senegal")
        site = request.query_params.get("site")
        zone = request.query_params.get("zone")
        status_filter = request.query_params.get("status")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 50))

        page = max(page, 1)
        limit = min(max(limit, 1), 200)

        month, start, end = _month_bounds(month)

        efms_qs = FuelEfmsMonthly.objects.filter(
            country=country,
            month_year=month,
        )

        enoc_qs = FuelEnocMovement.objects.filter(
            operation_date__gte=start,
            operation_date__lt=end,
        )

        if site:
            site_q = Q(site_id__icontains=site) | Q(site_name__icontains=site)
            efms_qs = efms_qs.filter(site_q)
            enoc_qs = enoc_qs.filter(site_q | Q(request_code__icontains=site))

        if zone:
            enoc_qs = enoc_qs.filter(zone__icontains=zone)

        efms_by_key = {}

        for row in efms_qs:
            key = row.site_id or row.site_name
            if not key:
                continue

            efms_by_key[key] = {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "country": row.country,
                "month_year": row.month_year,
                "fuel_order_l": _f(row.fuel_order_l),
                "fuel_deli_l": _f(row.fuel_deli_l),
                "fuel_conso_l": _f(row.fuel_conso_l),
                "ge_working_hours": _f(row.ge_working_hours),
                "abnormal_ge_working_hours": _f(row.abnormal_ge_working_hours),
                "monitoring_unavailability_hours": _f(row.monitoring_unavailability_hours),
                "monitoring_unavailability_percent": _f(row.monitoring_unavailability_percent),
                "cph_l_per_hour": _f(row.cph_l_per_hour),
                "anomaly_flags": row.anomaly_flags or [],
                "synced_at": row.synced_at,
            }

        enoc_by_key = {}

        enoc_values = enoc_qs.values(
            "site_id",
            "site_name",
            "zone",
            "ville",
            "request_code",
            "operation_type",
            "operation_date",
            "quantity_added_liters",
            "target_status",
        )

        for mov in enoc_values:
            key = mov.get("site_id") or mov.get("site_name")
            if not key:
                continue

            if key not in enoc_by_key:
                enoc_by_key[key] = {
                    "site_id": mov.get("site_id"),
                    "site_name": mov.get("site_name"),
                    "zone": mov.get("zone"),
                    "ville": mov.get("ville"),
                    "movements_count": 0,
                    "quantity_added_liters": 0.0,
                    "operation_types": set(),
                    "last_operation_date": None,
                    "last_request_code": None,
                    "target_status": None,
                }

            bucket = enoc_by_key[key]
            bucket["movements_count"] += 1
            bucket["quantity_added_liters"] += _f(mov.get("quantity_added_liters"))

            if mov.get("operation_type"):
                bucket["operation_types"].add(mov.get("operation_type"))

            op_date = mov.get("operation_date")
            if op_date and (
                bucket["last_operation_date"] is None
                or op_date > bucket["last_operation_date"]
            ):
                bucket["last_operation_date"] = op_date
                bucket["last_request_code"] = mov.get("request_code")
                bucket["target_status"] = mov.get("target_status")

        keys = sorted(set(efms_by_key.keys()) | set(enoc_by_key.keys()))
        rows = []

        totals = {
            "efms_sites": 0,
            "enoc_sites": 0,
            "fuel_order_l": 0.0,
            "fuel_deli_l": 0.0,
            "fuel_conso_l": 0.0,
            "enoc_quantity_added_liters": 0.0,
            "movements_count": 0,
            "ok": 0,
            "warning": 0,
            "nok": 0,
            "efms_only": 0,
            "enoc_only": 0,
        }

        for key in keys:
            efms = efms_by_key.get(key)
            enoc = enoc_by_key.get(key)

            has_efms = efms is not None
            has_enoc = enoc is not None

            efms_payload = efms or {
                "fuel_order_l": 0,
                "fuel_deli_l": 0,
                "fuel_conso_l": 0,
                "ge_working_hours": 0,
                "abnormal_ge_working_hours": 0,
                "monitoring_unavailability_hours": 0,
                "monitoring_unavailability_percent": 0,
                "cph_l_per_hour": 0,
                "anomaly_flags": [],
                "synced_at": None,
            }

            enoc_payload = enoc or {
                "zone": None,
                "ville": None,
                "movements_count": 0,
                "quantity_added_liters": 0,
                "operation_types": set(),
                "last_operation_date": None,
                "last_request_code": None,
                "target_status": None,
            }

            deli_gap_l, deli_gap_pct = _gap(
                efms_payload.get("fuel_deli_l"),
                enoc_payload.get("quantity_added_liters"),
            )

            conso_gap_l, conso_gap_pct = _gap(
                efms_payload.get("fuel_conso_l"),
                enoc_payload.get("quantity_added_liters"),
            )

            status = _row_status(has_efms, has_enoc, deli_gap_pct)

            site_id = (
                (efms or {}).get("site_id")
                or (enoc or {}).get("site_id")
            )
            site_name = (
                (efms or {}).get("site_name")
                or (enoc or {}).get("site_name")
            )

            row = {
                "key": key,
                "month_year": month,
                "site_id": site_id,
                "site_name": site_name,
                "zone": enoc_payload.get("zone"),
                "ville": enoc_payload.get("ville"),
                "source": (
                    "EFMS_ENOC" if has_efms and has_enoc
                    else "EFMS_ONLY" if has_efms
                    else "ENOC_ONLY" if has_enoc
                    else "NONE"
                ),
                "efms": {
                    "fuel_order_l": efms_payload.get("fuel_order_l", 0),
                    "fuel_deli_l": efms_payload.get("fuel_deli_l", 0),
                    "fuel_conso_l": efms_payload.get("fuel_conso_l", 0),
                    "ge_working_hours": efms_payload.get("ge_working_hours", 0),
                    "abnormal_ge_working_hours": efms_payload.get("abnormal_ge_working_hours", 0),
                    "monitoring_unavailability_hours": efms_payload.get("monitoring_unavailability_hours", 0),
                    "monitoring_unavailability_percent": efms_payload.get("monitoring_unavailability_percent", 0),
                    "cph_l_per_hour": efms_payload.get("cph_l_per_hour", 0),
                    "anomaly_flags": efms_payload.get("anomaly_flags", []),
                    "synced_at": efms_payload.get("synced_at"),
                },
                "enoc": {
                    "movements_count": enoc_payload.get("movements_count", 0),
                    "quantity_added_liters": round(enoc_payload.get("quantity_added_liters", 0), 3),
                    "operation_types": sorted(list(enoc_payload.get("operation_types", set()))),
                    "last_operation_date": enoc_payload.get("last_operation_date"),
                    "last_request_code": enoc_payload.get("last_request_code"),
                    "target_status": enoc_payload.get("target_status"),
                },
                "gaps": {
                    "deli_vs_enoc_l": deli_gap_l,
                    "deli_vs_enoc_pct": deli_gap_pct,
                    "conso_vs_enoc_l": conso_gap_l,
                    "conso_vs_enoc_pct": conso_gap_pct,
                    "status": status,
                },
            }

            code = status["code"]
            if code == "OK":
                totals["ok"] += 1
            elif code == "WARNING":
                totals["warning"] += 1
            elif code == "NOK":
                totals["nok"] += 1
            elif code == "EFMS_ONLY":
                totals["efms_only"] += 1
            elif code == "ENOC_ONLY":
                totals["enoc_only"] += 1

            if has_efms:
                totals["efms_sites"] += 1
                totals["fuel_order_l"] += efms_payload.get("fuel_order_l", 0)
                totals["fuel_deli_l"] += efms_payload.get("fuel_deli_l", 0)
                totals["fuel_conso_l"] += efms_payload.get("fuel_conso_l", 0)

            if has_enoc:
                totals["enoc_sites"] += 1
                totals["enoc_quantity_added_liters"] += enoc_payload.get("quantity_added_liters", 0)
                totals["movements_count"] += enoc_payload.get("movements_count", 0)

            rows.append(row)

        if status_filter and status_filter != "ALL":
            rows = [
                r for r in rows
                if r["gaps"]["status"]["code"] == status_filter
            ]

        rows = sorted(rows, key=lambda r: r.get("site_id") or r.get("site_name") or "")

        total = len(rows)
        total_pages = ceil(total / limit) if total else 1
        start_idx = (page - 1) * limit
        end_idx = start_idx + limit

        return Response({
            "filters": {
                "month": month,
                "country": country,
                "site": site,
                "zone": zone,
                "status": status_filter or "ALL",
            },
            "kpis": {
                **{k: round(v, 3) if isinstance(v, float) else v for k, v in totals.items()},
                "total_sites": total,
                "gap_deli_vs_enoc_l": round(totals["enoc_quantity_added_liters"] - totals["fuel_deli_l"], 3),
                "gap_conso_vs_enoc_l": round(totals["enoc_quantity_added_liters"] - totals["fuel_conso_l"], 3),
            },
            "data": rows[start_idx:end_idx],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
        })


class FuelEnocJournalView(APIView):
    """
    Journal ENOC : mouvements terrain importés depuis ENOC.
    """

    def get(self, request):
        month = request.query_params.get("month")
        site = request.query_params.get("site")
        zone = request.query_params.get("zone")
        operation_type = request.query_params.get("operation_type")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 50))

        page = max(page, 1)
        limit = min(max(limit, 1), 200)

        qs = FuelEnocMovement.objects.all().order_by("-operation_date", "-id")

        if month:
            month, start, end = _month_bounds(month)
            qs = qs.filter(operation_date__gte=start, operation_date__lt=end)

        if site:
            qs = qs.filter(
                Q(site_id__icontains=site)
                | Q(site_name__icontains=site)
                | Q(request_code__icontains=site)
            )

        if zone:
            qs = qs.filter(zone__icontains=zone)

        if operation_type and operation_type != "ALL":
            qs = qs.filter(operation_type=operation_type)

        total = qs.count()
        total_pages = ceil(total / limit) if total else 1

        rows = qs[(page - 1) * limit: page * limit]
        serializer = FuelEnocMovementSerializer(rows, many=True)

        total_quantity = sum(_f(x.quantity_added_liters) for x in qs)
        total_movements = total

        return Response({
            "summary": {
                "total_movements": total_movements,
                "total_quantity_added_liters": round(total_quantity, 3),
            },
            "data": serializer.data,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
        })


class FuelSyncRunsView(APIView):
    """
    Historique des synchronisations eFMS et ENOC.
    """

    def get(self, request):
        efms = FuelEfmsSyncRun.objects.all().order_by("-started_at")[:10]
        enoc = FuelEnocSyncRun.objects.all().order_by("-started_at")[:10]

        return Response({
            "efms": FuelEfmsSyncRunSerializer(efms, many=True).data,
            "enoc": FuelEnocSyncRunSerializer(enoc, many=True).data,
        })



class FuelEfmsSyncRunListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = FuelEfmsSyncRun.objects.all().order_by("-started_at")[:20]
        return Response({
            "data": FuelEfmsSyncRunSerializer(qs, many=True).data
        })