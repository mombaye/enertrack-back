# fuel_tracking/views.py

import logging
from datetime import datetime, time
from decimal import Decimal
from math import ceil

logger = logging.getLogger(__name__)

from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django.apps import apps
from django.conf import settings


from fuel_tracking.models import (
    FuelEfmsMonthly,
    FuelEfmsSyncRun,
    FuelEnocMovement,
    FuelEnocSyncRun,
    FuelSiteScope,
)
from fuel_tracking.serializers import (
    FuelEfmsMonthlySerializer,
    FuelEfmsSyncRunSerializer,
    FuelEnocMovementSerializer,
    FuelEnocSyncRunSerializer,
)


ANOMALY_CODES = [
    "DELIVERY_GT_ORDER",
    "CONSO_WITHOUT_GE_HOURS",
    "GE_HOURS_WITHOUT_CONSO",
    "ABNORMAL_GE_HOURS",
    "HIGH_MONITORING_UNAVAILABILITY",
]

SITE_MODEL_CACHE = None


def _get_site_model():
    """
    Récupère dynamiquement le modèle Site.

    Recommandé :
    Dans settings.py, tu peux ajouter :
    FUEL_TRACKING_SITE_MODEL = "sites.Site"

    Si ce setting n'existe pas, on cherche automatiquement un modèle nommé Site
    avec les champs site_id, zone, country.
    """
    global SITE_MODEL_CACHE

    if SITE_MODEL_CACHE is not None:
        return SITE_MODEL_CACHE

    dotted = getattr(settings, "FUEL_TRACKING_SITE_MODEL", None)

    if dotted:
        app_label, model_name = dotted.split(".")
        SITE_MODEL_CACHE = apps.get_model(app_label, model_name)
        return SITE_MODEL_CACHE

    for model in apps.get_models():
        field_names = {field.name for field in model._meta.fields}

        if (
            model.__name__ == "Site"
            and {"site_id", "zone", "country"}.issubset(field_names)
        ):
            SITE_MODEL_CACHE = model
            return SITE_MODEL_CACHE

    return None


def _safe_attr(obj, attr, default=None):
    return getattr(obj, attr, default)


def _display(obj, method_name, fallback=None):
    method = getattr(obj, method_name, None)

    if callable(method):
        try:
            return method()
        except Exception:
            return fallback

    return fallback


def _site_to_ref(site):
    """
    Transforme un objet Site en payload exploitable côté Fuel Tracking.
    """
    if not site:
        return None

    return {
        "site_id": _safe_attr(site, "site_id"),
        "name": _safe_attr(site, "name"),

        "zone": _safe_attr(site, "zone"),
        "zone_label": _display(site, "get_zone_display", _safe_attr(site, "zone")),

        "country": _safe_attr(site, "country"),
        "country_label": _display(site, "get_country_display", _safe_attr(site, "country")),

        "modernized": _safe_attr(site, "modernized"),
        "ordered_typology": _safe_attr(site, "ordered_typology"),
        "installed_typology": _safe_attr(site, "installed_typology"),
        "billing_typology": _safe_attr(site, "billing_typology"),

        "contract_number": _safe_attr(site, "contract_number"),
        "meter_number": _safe_attr(site, "meter_number"),

        "analysis_load": _safe_attr(site, "analysis_load"),
        "load_band": _safe_attr(site, "load_band"),

        "site_type": _safe_attr(site, "site_type"),
        "ordered_site_type": _safe_attr(site, "ordered_site_type"),
        "installed_site_type": _safe_attr(site, "installed_site_type"),

        "configuration": _safe_attr(site, "configuration"),
        "target_mapping_key": _safe_attr(site, "target_mapping_key"),
        "transformer_capacity": _safe_attr(site, "transformer_capacity"),

        "indoor_billed_outdoor": _safe_attr(site, "indoor_billed_outdoor"),
        "not_yet_solarized": _safe_attr(site, "not_yet_solarized"),
        "solarization_date": (
            _safe_attr(site, "solarization_date").isoformat()
            if _safe_attr(site, "solarization_date")
            else None
        ),

        "energy_desk_comment": _safe_attr(site, "energy_desk_comment"),
        "load_comment_category": _safe_attr(site, "load_comment_category"),

        "invoice_payment": _safe_attr(site, "invoice_payment"),
        "grid_fee": _safe_attr(site, "grid_fee"),
        "batch_operational": _safe_attr(site, "batch_operational"),

        "scope_status": _safe_attr(site, "scope_status"),
        "meter_status": _safe_attr(site, "meter_status"),
    }


def _load_site_refs(site_ids):
    """
    Charge les sites en une seule requête pour éviter le N+1.
    """
    Site = _get_site_model()

    if not Site:
        return {}

    clean_ids = sorted({
        str(site_id).strip()
        for site_id in site_ids
        if site_id and str(site_id).strip()
    })

    if not clean_ids:
        return {}

    qs = Site.objects.filter(site_id__in=clean_ids)

    return {
        site.site_id: _site_to_ref(site)
        for site in qs
    }


def _load_enoc_mongo_refs(site_ids):
    """
    Référentiel site/GE (marque, KVA, cuve, priorité...) lu directement dans
    MongoDB ENOC — indépendant des mouvements carburant du mois, contourne
    l'API REST ENOC (en panne). Tolérant aux pannes : retourne des dicts vides
    si MongoDB est injoignable plutôt que de casser tout l'endpoint.
    """
    clean_ids = sorted({str(sid).strip() for sid in site_ids if sid and str(sid).strip()})
    if not clean_ids:
        return {}, {}, {}

    try:
        from fuel_tracking.services.enoc_mongo_service import EnocMongoService
        from fuel_tracking.services.genset_curve_matching import match_genset_curve
        from fuel_tracking.services.cph_matrix_matching import match_cph_target

        svc = EnocMongoService()
        ge_assets = svc.get_ge_assets(clean_ids)

        for assets in ge_assets.values():
            for asset in assets:
                asset["fuel_curve"] = match_genset_curve(
                    asset.get("brand"), asset.get("power_kva"), asset.get("model")
                )
                asset["cph_target"] = match_cph_target(asset.get("brand"), asset.get("power_kva"))

        return svc.get_sites_ref(clean_ids), ge_assets, svc.get_fuel_targets(clean_ids)
    except Exception as e:
        logger.warning("[fuel_tracking] Référentiel MongoDB ENOC indisponible: %s", e)
        return {}, {}, {}


def _load_rh_initial(country, site_ids, prev_month, prev_start, prev_end):
    """
    RH Initial = RH calculé du mois précédent (même cascade que RH Final).
    Lit uniquement la valeur déjà persistée (FuelEfmsMonthly du mois M-1, via le
    cron nocturne sync_efms_fuel_auto) — PAS de repli Snowflake live ici : mesuré
    à ~3s par appel Snowflake (GFMS_DATA_TRACKER_NC), quasi indépendant du nombre
    de sites (coût fixe, pas de gain à limiter la taille du lot) — c'était la
    cause principale des pages Suivi Carburant très lentes au filtrage par date.
    Même principe déjà appliqué à Suivi Conso (Grid/ACM) : aucun appel Snowflake
    live dans le chemin de requête HTTP. Un site dont le mois précédent n'a pas
    encore été synchronisé affiche "—" plutôt que de bloquer la page.
    """
    clean_ids = sorted({str(sid).strip() for sid in site_ids if sid and str(sid).strip()})
    if not clean_ids:
        return {}

    result = {}
    persisted = FuelEfmsMonthly.objects.filter(
        country__iexact=country, month_year=prev_month, site_id__in=clean_ids
    ).values("site_id", "rh_hours", "rh_source")
    for row in persisted:
        if row["rh_hours"] is not None:
            result[row["site_id"]] = {"rh_hours": row["rh_hours"], "source": row["rh_source"]}

    return result


def _load_stock_rms(country, month, site_ids):
    """
    Stock Ouv./Clôt. RMS — niveau de cuve, télémétrie Snowflake (GFMS_DATA_TRACKER_NC).

    Lit uniquement la valeur déjà persistée (FuelEfmsMonthly du mois affiché, via
    le cron nocturne sync_efms_fuel_auto) — PAS de repli Snowflake live ici, même
    principe que _load_rh_initial : mesuré à ~29s/200 sites en requête directe sur
    cette table brute, c'était l'une des deux causes des pages Suivi Carburant très
    lentes au filtrage par date. Un site dont le mois n'a pas encore été synchronisé
    affiche "—" plutôt que de bloquer la page.
    """
    clean_ids = sorted({str(sid).strip() for sid in site_ids if sid and str(sid).strip()})
    if not clean_ids:
        return {}

    result = {}
    persisted = FuelEfmsMonthly.objects.filter(
        country__iexact=country, month_year=month, site_id__in=clean_ids
    ).values("site_id", "stock_ouv_rms_l", "stock_clot_rms_l")
    for row in persisted:
        if row["stock_ouv_rms_l"] is not None or row["stock_clot_rms_l"] is not None:
            result[row["site_id"]] = {
                "ouv_rms": row["stock_ouv_rms_l"],
                "clot_rms": row["stock_clot_rms_l"],
            }

    return result


def _load_prelevement_out(site_ids, start, end):
    """
    Prélèvement Out = ponction SORTANTE : mouvements PONCTION dont le site
    source (ponction.source_site_id) est l'un des sites affichés — cherché
    dans TOUS les mouvements ENOC du mois, pas seulement ceux du site lui-même.
    """
    clean_ids = sorted({str(sid).strip() for sid in site_ids if sid and str(sid).strip()})
    if not clean_ids:
        return {}

    result = {sid: 0.0 for sid in clean_ids}
    movements = FuelEnocMovement.objects.filter(
        operation_type="PONCTION",
        ponction__source_site_id__in=clean_ids,
        operation_date__gte=start,
        operation_date__lt=end,
    ).values("ponction", "quantity_added_liters")

    for mov in movements:
        source_id = (mov.get("ponction") or {}).get("source_site_id")
        if source_id in result:
            result[source_id] += _f(mov.get("quantity_added_liters"))

    return result


def _to_float(value):
    if value is None:
        return 0
    try:
        return float(value)
    except Exception:
        return 0


def _f(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def _paginate_queryset(qs, request):
    page = int(request.query_params.get("page", 1))
    limit = int(request.query_params.get("limit", 50))

    page = max(page, 1)
    limit = min(max(limit, 10), 500)

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


def _month_bounds(month_year: str | None):
    """
    Retourne :
    - month_year normalisé YYYY-MM
    - start datetime aware
    - end datetime aware
    """
    if month_year:
        parsed = parse_date(f"{month_year}-01")
    else:
        today = timezone.localdate()
        month_year = f"{today.year}-{today.month:02d}"
        parsed = parse_date(f"{month_year}-01")

    if not parsed:
        today = timezone.localdate()
        month_year = f"{today.year}-{today.month:02d}"
        parsed = parse_date(f"{month_year}-01")

    year = parsed.year
    month = parsed.month

    if month == 12:
        end_date = parse_date(f"{year + 1}-01-01")
    else:
        end_date = parse_date(f"{year}-{month + 1:02d}-01")

    tz = timezone.get_current_timezone()

    start_dt = timezone.make_aware(datetime.combine(parsed, time.min), tz)
    end_dt = timezone.make_aware(datetime.combine(end_date, time.min), tz)

    return month_year, start_dt, end_dt


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


def _gap(value_a, value_b):
    """
    Retourne :
    - diff = ENOC - eFMS
    - pct basé sur eFMS
    """
    a = _f(value_a)
    b = _f(value_b)

    diff = b - a

    if a <= 0 and b <= 0:
        return None, None

    if a <= 0 and b > 0:
        return round(diff, 3), 100.0

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

        anomalies = {}
        for code in ANOMALY_CODES:
            anomalies[code] = qs.filter(anomaly_flags__contains=[code]).count()

        top_conso = qs.filter(fuel_conso_l__gt=0).order_by("-fuel_conso_l")[:10]
        top_cph = qs.filter(cph_l_per_hour__isnull=False).order_by("-cph_l_per_hour")[:10]
        top_ge_hours = qs.filter(ge_working_hours__gt=0).order_by("-ge_working_hours")[:10]

        country = request.query_params.get("country", "Senegal")

        monthly_evolution = (
            FuelEfmsMonthly.objects
            .filter(country__iexact=country)
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
                "country": country,
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


class FuelMonthlyTrackingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        month = request.query_params.get("month")
        country = request.query_params.get("country", "Senegal")
        site = request.query_params.get("site")
        zone = request.query_params.get("zone")
        status_filter = request.query_params.get("status")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 50))
        include_out_of_scope = request.query_params.get("include_out_of_scope") == "true"

        page = max(page, 1)
        limit = min(max(limit, 1), 500)

        month, start, end = _month_bounds(month)

        _year, _mon = (int(x) for x in month.split("-"))
        prev_month_str = f"{_year - 1}-12" if _mon == 1 else f"{_year}-{_mon - 1:02d}"
        prev_month, prev_start, prev_end = _month_bounds(prev_month_str)

        efms_qs = FuelEfmsMonthly.objects.filter(
            country__iexact=country,
            month_year=month,
        )

        enoc_qs = FuelEnocMovement.objects.filter(
            operation_date__gte=start,
            operation_date__lt=end,
        )

        # Périmètre : par défaut on ne montre que les sites avec GE installé
        # (cf. FuelSiteScope) — le parc complet inclut des sites On-Grid sans
        # genset qui n'ont rien à suivre côté fuel. ?include_out_of_scope=true
        # permet de voir tout le parc (debug/admin).
        if not include_out_of_scope:
            in_scope_ids = set(
                FuelSiteScope.objects.filter(has_genset=True).values_list("site_id", flat=True)
            )
            if in_scope_ids:
                scope_q = Q(site_id__in=in_scope_ids) | Q(site_id__isnull=True) | Q(site_id="")
                efms_qs = efms_qs.filter(scope_q)
                enoc_qs = enoc_qs.filter(scope_q)

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
                "rh_hours": _f(row.rh_hours) if row.rh_hours is not None else None,
                "rh_source": row.rh_source,
                "avec_dse": row.avec_dse,
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
            "raw_payload",
            "ge_snapshot",
            "level_before",
            "level_after",
            "ponction",
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
                    "refueling_liters": 0.0,
                    "ajout_in_liters": 0.0,
                    "operation_types": set(),
                    "first_operation_date": None,
                    "first_level_before": None,
                    "last_operation_date": None,
                    "last_request_code": None,
                    "last_level_after": None,
                    "target_status": None,
                    "site_context": None,
                    "ge_context": None,
                    "ge_snapshot": None,
                }

            bucket = enoc_by_key[key]
            raw_payload = mov.get("raw_payload") or {}

            if raw_payload.get("site_context") and not bucket.get("site_context"):
                bucket["site_context"] = raw_payload.get("site_context")

            if raw_payload.get("ge_context") and not bucket.get("ge_context"):
                bucket["ge_context"] = raw_payload.get("ge_context")

            if mov.get("ge_snapshot") and not bucket.get("ge_snapshot"):
                bucket["ge_snapshot"] = mov.get("ge_snapshot")
            bucket["movements_count"] += 1
            bucket["quantity_added_liters"] += _f(mov.get("quantity_added_liters"))

            op_type = mov.get("operation_type")
            ponction_data = mov.get("ponction") or {}
            # Ponction sortante non résolue (destinataire inconnu) : le mouvement est
            # filé sous le site source lui-même (source_site_id == site_id), ce n'est
            # pas un ajout pour ce site — seul _load_prelevement_out doit la compter.
            is_unresolved_ponction_out = (
                op_type == "PONCTION" and ponction_data.get("source_site_id") == mov.get("site_id")
            )

            if op_type == "TRUCK":
                # Dépotage — livraison camion classique.
                bucket["refueling_liters"] += _f(mov.get("quantity_added_liters"))
            elif op_type in ("TOTAL_CARD", "PONCTION") and not is_unresolved_ponction_out:
                # Ajout sur le site : carte carburant, ou ponction reçue d'un autre site.
                bucket["ajout_in_liters"] += _f(mov.get("quantity_added_liters"))

            if mov.get("operation_type"):
                bucket["operation_types"].add(mov.get("operation_type"))

            op_date = mov.get("operation_date")

            if op_date and (
                bucket["first_operation_date"] is None
                or op_date < bucket["first_operation_date"]
            ):
                bucket["first_operation_date"] = op_date
                bucket["first_level_before"] = mov.get("level_before")

            if op_date and (
                bucket["last_operation_date"] is None
                or op_date > bucket["last_operation_date"]
            ):
                bucket["last_operation_date"] = op_date
                bucket["last_request_code"] = mov.get("request_code")
                bucket["target_status"] = mov.get("target_status")
                bucket["last_level_after"] = mov.get("level_after")

        keys = sorted(set(efms_by_key.keys()) | set(enoc_by_key.keys()))
        rows = []

        site_ids = set()

        for item in efms_by_key.values():
            if item.get("site_id"):
                site_ids.add(item.get("site_id"))

        for item in enoc_by_key.values():
            if item.get("site_id"):
                site_ids.add(item.get("site_id"))

        site_refs = _load_site_refs(site_ids)
        mongo_site_refs, mongo_ge_assets, mongo_fuel_targets = _load_enoc_mongo_refs(site_ids)
        # RH Initial (repli Snowflake live pour les sites sans mois précédent synced) et
        # Stock RMS sont calculés plus bas, uniquement pour les sites de la page affichée —
        # pas pour tout le périmètre (jusqu'à ~467+ sites) : les deux finissent par
        # interroger GFMS_DATA_TRACKER_NC, une table de télémétrie brute coûteuse
        # (mesuré : ~19s/467 sites pour RH Initial, ~29s/200 sites pour Stock RMS) —
        # ça n'a pas de sens de la requêter pour des sites qui ne seront même pas affichés.
        rh_initial_by_site: dict = {}
        stock_rms_by_site: dict = {}
        prelevement_out_by_site = _load_prelevement_out(site_ids, start, end)
        zone_filter = str(zone or "").strip().lower()

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
                "rh_hours": None,
                "rh_source": None,
                "avec_dse": None,
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

            site_id = (efms or {}).get("site_id") or (enoc or {}).get("site_id")
            site_name = (efms or {}).get("site_name") or (enoc or {}).get("site_name")

            site_ref = site_refs.get(site_id)
            enoc_site_ref = enoc_payload.get("site_context") or {}
            enoc_ge_ref = enoc_payload.get("ge_context") or {}
            enoc_ge_snapshot = enoc_payload.get("ge_snapshot") or {}

            # Référentiel MongoDB ENOC (marque GE, KVA, cuve, priorité...) — indépendant
            # des mouvements du mois, prioritaire sur ce qui vient du raw_payload.
            mongo_site = mongo_site_refs.get(site_id)
            if mongo_site:
                enoc_site_ref = {**enoc_site_ref, **{k: v for k, v in mongo_site.items() if v is not None}}

            mongo_assets = mongo_ge_assets.get(site_id) or []
            if mongo_assets:
                enoc_ge_ref = {
                    "assets_count": len(mongo_assets),
                    "assets": mongo_assets,
                    "primary_asset": mongo_assets[0],
                }

            if site_ref:
                site_name = site_ref.get("name") or site_name
            else:
                site_name = enoc_site_ref.get("site_name") or site_name

            site_zone = (
                (site_ref or {}).get("zone")
                or enoc_site_ref.get("zone")
                or enoc_site_ref.get("region")
                or enoc_payload.get("zone")
            )

            site_zone_label = (
                (site_ref or {}).get("zone_label")
                or enoc_site_ref.get("region")
                or enoc_site_ref.get("zone")
                or enoc_payload.get("zone")
            )

            if site_ref:
                site_name = site_ref.get("name") or site_name

            site_zone = (
                (site_ref or {}).get("zone")
                or enoc_payload.get("zone")
            )

            site_zone_label = (
                (site_ref or {}).get("zone_label")
                or enoc_payload.get("zone")
            )

            if zone_filter:
                zone_candidates = [
                    site_zone,
                    site_zone_label,
                    enoc_payload.get("zone"),
                ]

                if not any(
                    zone_filter in str(candidate).lower()
                    for candidate in zone_candidates
                    if candidate
                ):
                    continue

            row = {
                "key": key,
                "month_year": month,
                "site_id": site_id,
                "site_name": site_name,
                "zone": site_zone,
                "zone_label": site_zone_label,
                "ville": enoc_payload.get("ville"),
                "site_ref": site_ref,
                "enoc_site_ref": enoc_site_ref or None,
                "ge_ref": enoc_ge_ref or None,
                "ge_snapshot": enoc_ge_snapshot or None,
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
                    "rh_hours": efms_payload.get("rh_hours"),
                    "rh_source": efms_payload.get("rh_source"),
                    "avec_dse": efms_payload.get("avec_dse"),
                    "rh_initial_hours": (rh_initial_by_site.get(site_id) or {}).get("rh_hours"),
                    "rh_initial_source": (rh_initial_by_site.get(site_id) or {}).get("source"),
                    "rh_delta_hours": (
                        float(efms_payload.get("rh_hours")) - float(rh_initial_by_site[site_id]["rh_hours"])
                        if efms_payload.get("rh_hours") is not None
                        and (rh_initial_by_site.get(site_id) or {}).get("rh_hours") is not None
                        else None
                    ),
                    "synced_at": efms_payload.get("synced_at"),
                },
                "enoc": {
                    "movements_count": enoc_payload.get("movements_count", 0),
                    "quantity_added_liters": round(enoc_payload.get("quantity_added_liters", 0), 3),
                    "refueling_liters": round(enoc_payload.get("refueling_liters", 0), 3),
                    "ajout_in_liters": round(enoc_payload.get("ajout_in_liters", 0), 3),
                    "prelevement_out_liters": round(prelevement_out_by_site.get(site_id, 0), 3),
                    "operation_types": sorted(list(enoc_payload.get("operation_types", set()))),
                    "last_operation_date": enoc_payload.get("last_operation_date"),
                    "last_request_code": enoc_payload.get("last_request_code"),
                    "target_status": enoc_payload.get("target_status"),
                    "monthly_target_liters": mongo_fuel_targets.get(site_id),
                    "site_context": enoc_site_ref or None,
                    "ge_context": enoc_ge_ref or None,
                    "ge_snapshot": enoc_ge_snapshot or None,
                },
                "stock": {
                    "ouv_rms": (stock_rms_by_site.get(site_id) or {}).get("ouv_rms"),
                    "clot_rms": (stock_rms_by_site.get(site_id) or {}).get("clot_rms"),
                    "delta_rms": (
                        stock_rms_by_site[site_id]["clot_rms"] - stock_rms_by_site[site_id]["ouv_rms"]
                        if stock_rms_by_site.get(site_id)
                        and stock_rms_by_site[site_id]["clot_rms"] is not None
                        and stock_rms_by_site[site_id]["ouv_rms"] is not None
                        else None
                    ),
                    "ouv_reel": enoc_payload.get("first_level_before"),
                    "clot_reel": enoc_payload.get("last_level_after"),
                    "reel": enoc_payload.get("last_level_after"),
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
        page_rows = rows[start_idx:end_idx]

        # RH Initial + Stock RMS : calculés ici seulement, pour les sites réellement
        # affichés sur cette page (voir commentaire plus haut sur le coût de
        # GFMS_DATA_TRACKER_NC).
        page_site_ids = {r["site_id"] for r in page_rows if r.get("site_id")}

        rh_initial_by_site = _load_rh_initial(country, page_site_ids, prev_month, prev_start, prev_end)
        stock_rms_by_site = _load_stock_rms(country, month, page_site_ids)

        for r in page_rows:
            site_id = r.get("site_id")

            rh_initial = rh_initial_by_site.get(site_id)
            if rh_initial:
                r["efms"]["rh_initial_hours"] = rh_initial.get("rh_hours")
                r["efms"]["rh_initial_source"] = rh_initial.get("source")
                if r["efms"].get("rh_hours") is not None and rh_initial.get("rh_hours") is not None:
                    r["efms"]["rh_delta_hours"] = float(r["efms"]["rh_hours"]) - float(rh_initial["rh_hours"])

            rms = stock_rms_by_site.get(site_id)
            if rms:
                r["stock"]["ouv_rms"] = rms.get("ouv_rms")
                r["stock"]["clot_rms"] = rms.get("clot_rms")
                r["stock"]["delta_rms"] = (
                    rms["clot_rms"] - rms["ouv_rms"]
                    if rms.get("clot_rms") is not None and rms.get("ouv_rms") is not None
                    else None
                )

        return Response({
            "filters": {
                "month": month,
                "country": country,
                "site": site,
                "zone": zone,
                "status": status_filter or "ALL",
            },
            "kpis": {
                **{
                    k: round(v, 3) if isinstance(v, float) else v
                    for k, v in totals.items()
                },
                "total_sites": total,
                "gap_deli_vs_enoc_l": round(
                    totals["enoc_quantity_added_liters"] - totals["fuel_deli_l"],
                    3,
                ),
                "gap_conso_vs_enoc_l": round(
                    totals["enoc_quantity_added_liters"] - totals["fuel_conso_l"],
                    3,
                ),
            },
            "data": page_rows,
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
    permission_classes = [IsAuthenticated]

    def get(self, request):
        month = request.query_params.get("month")
        site = request.query_params.get("site")
        zone = request.query_params.get("zone")
        operation_type = request.query_params.get("operation_type")
        page = int(request.query_params.get("page", 1))
        limit = int(request.query_params.get("limit", 50))

        page = max(page, 1)
        limit = min(max(limit, 1), 500)

        qs = FuelEnocMovement.objects.all().order_by("-operation_date", "-id")

        if month:
            _, start, end = _month_bounds(month)
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

        total_quantity = qs.aggregate(v=Sum("quantity_added_liters"))["v"] or 0

        return Response({
            "summary": {
                "total_movements": total,
                "total_quantity_added_liters": round(_f(total_quantity), 3),
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


class FuelSourceStatusView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        country = request.query_params.get("country", "Senegal")
        requested_month = request.query_params.get("month")

        efms_qs = FuelEfmsMonthly.objects.filter(country__iexact=country)
        enoc_qs = FuelEnocMovement.objects.all()

        latest_efms_month = efms_qs.aggregate(v=Max("month_year"))["v"]

        efms_last_month_count = 0
        efms_last_month_sites = 0

        if latest_efms_month:
            last_month_qs = efms_qs.filter(month_year=latest_efms_month)
            efms_last_month_count = last_month_qs.count()
            efms_last_month_sites = last_month_qs.values("site_id").distinct().count()

        latest_enoc_operation = enoc_qs.aggregate(v=Max("operation_date"))["v"]

        requested_month_info = None

        if requested_month:
            _, start, end = _month_bounds(requested_month)

            efms_requested_count = efms_qs.filter(month_year=requested_month).count()
            enoc_requested_count = enoc_qs.filter(
                operation_date__gte=start,
                operation_date__lt=end,
            ).count()

            requested_month_info = {
                "month": requested_month,
                "has_efms": efms_requested_count > 0,
                "has_enoc": enoc_requested_count > 0,
                "efms_rows": efms_requested_count,
                "enoc_movements": enoc_requested_count,
                "status": (
                    "EFMS_ENOC" if efms_requested_count > 0 and enoc_requested_count > 0
                    else "EFMS_ONLY" if efms_requested_count > 0
                    else "ENOC_ONLY" if enoc_requested_count > 0
                    else "NO_DATA"
                ),
            }

        return Response({
            "country": country,
            "efms": {
                "available": bool(latest_efms_month),
                "latest_month": latest_efms_month,
                "latest_month_rows": efms_last_month_count,
                "latest_month_sites": efms_last_month_sites,
                "total_rows": efms_qs.count(),
                "total_sites": efms_qs.values("site_id").distinct().count(),
            },
            "enoc": {
                "available": enoc_qs.exists(),
                "latest_operation_date": latest_enoc_operation,
                "total_movements": enoc_qs.count(),
                "total_sites": enoc_qs.exclude(site_id__isnull=True).values("site_id").distinct().count(),
            },
            "requested_month": requested_month_info,
        })


class FuelSyncRunsView(APIView):
    permission_classes = [IsAuthenticated]

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




class FuelSiteReferenceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        Site = _get_site_model()

        if not Site:
            return Response({
                "available": False,
                "data": [],
                "message": "Modèle Site introuvable. Configure FUEL_TRACKING_SITE_MODEL dans settings.py.",
            })

        search = request.query_params.get("search")
        zone = request.query_params.get("zone")
        scope_status = request.query_params.get("scope_status")
        limit = int(request.query_params.get("limit", 50))

        limit = min(max(limit, 1), 200)

        qs = Site.objects.all()

        if search:
            qs = qs.filter(
                Q(site_id__icontains=search)
                | Q(name__icontains=search)
                | Q(contract_number__icontains=search)
                | Q(meter_number__icontains=search)
            )

        if zone:
            qs = qs.filter(zone__icontains=zone)

        if scope_status:
            qs = qs.filter(scope_status=scope_status)

        qs = qs.order_by("site_id")[:limit]

        return Response({
            "available": True,
            "data": [_site_to_ref(site) for site in qs],
        })


class FuelCphMatrixView(APIView):
    """
    Matrice CPH brute (feuille "CPH" du fichier Suivi Ravitaillement) —
    conso L/h par famille moteur / puissance kVA / % de charge. Voir
    fuel_tracking.services.cph_matrix_matching pour le matching par site.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import CphMatrixPoint

        points = CphMatrixPoint.objects.all().order_by(
            "engine_family_normalized", "dg_capacity_kva", "charge_pct"
        )

        engines: dict[str, dict[float, dict[str, float]]] = {}

        for p in points:
            rows = engines.setdefault(p.engine_family, {})
            values = rows.setdefault(float(p.dg_capacity_kva), {})
            pct_label = f"{float(p.charge_pct):g}"
            values[pct_label] = float(p.cph_l_h)

        data = [
            {
                "engine_family": engine_family,
                "rows": [
                    {"dg_capacity_kva": kva, "values": values}
                    for kva, values in sorted(kva_rows.items())
                ],
            }
            for engine_family, kva_rows in sorted(engines.items())
        ]

        return Response({"data": data})


class FuelTrackingExportView(APIView):
    """
    GET /api/fuel-tracking/export/?month=YYYY-MM&country=Senegal

    Génère le classeur Excel "Suivi Carburant" côté serveur (openpyxl), au
    format du modèle client — voir fuel_tracking/export.py. Réutilise les
    mêmes vues internes (FuelMonthlyTrackingView, FuelEnocJournalView,
    FuelCphMatrixView) que le frontend pour garantir que l'export affiche
    exactement les mêmes chiffres que l'écran.
    """
    permission_classes = [IsAuthenticated]

    def _call(self, view_cls, params: dict):
        from django.test import RequestFactory
        from rest_framework.request import Request as DRFRequest

        factory = RequestFactory()
        django_request = factory.get("/", data=params)
        return view_cls().get(DRFRequest(django_request))

    def _fetch_all_pages(self, view_cls, base_params: dict) -> list[dict]:
        all_rows = []
        page = 1
        while True:
            resp = self._call(view_cls, {**base_params, "page": page, "limit": 500})
            all_rows.extend(resp.data.get("data", []))
            pagination = resp.data.get("pagination") or {}
            if not pagination.get("hasNext"):
                break
            page += 1
        return all_rows, resp.data.get("kpis", {})

    def get(self, request):
        from django.http import HttpResponse

        from .export import build_fuel_tracking_workbook

        month = request.query_params.get("month")
        country = request.query_params.get("country", "Senegal")

        monthly_rows, kpis = self._fetch_all_pages(FuelMonthlyTrackingView, {"month": month, "country": country})
        journal_rows, _ = self._fetch_all_pages(FuelEnocJournalView, {"month": month})
        cph_resp = self._call(FuelCphMatrixView, {})
        cph_data = cph_resp.data.get("data", [])

        resolved_month = monthly_rows[0]["month_year"] if monthly_rows else (month or "")

        xlsx_bytes = build_fuel_tracking_workbook(
            month=resolved_month,
            monthly_rows=monthly_rows,
            journal_rows=journal_rows,
            kpis=kpis,
            cph_data=cph_data,
        )

        response = HttpResponse(
            xlsx_bytes,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        filename = f"suivi_carburant_{resolved_month or 'export'}.xlsx"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response