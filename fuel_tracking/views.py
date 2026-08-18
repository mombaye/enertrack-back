# fuel_tracking/views.py
#
# Module suivi-carburant : automatisé, plus d'import manuel de fichier.
# 4 sous-parties construites étape par étape (Dashboard résumé, Consommation,
# Stock, Commande) — chacune alimentée par sa propre synchronisation
# (Snowflake/ENOC pour l'instant, voir fuel_tracking/services/). Les anciens
# modèles liés à l'import manuel (FuelCommandeSynthese, FuelSuiviCommandeSite)
# restent en base pour l'historique mais ne sont plus exposés via l'API.

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class FuelConsommationListView(APIView):
    """
    GET /api/fuel-tracking/consommation/?month=YYYY-MM&search=&country=&has_genset=&page=&limit=

    Liste paginée de la Consommation carburant mensuelle par site
    (FuelConsommationMonthly) — automatisée (Snowflake + ENOC), voir
    sync_fuel_consommation. Sans mois demandé, retourne le mois le plus
    récent disponible. `search` filtre sur site_id/site_name (icontains).
    `has_genset=true|false` filtre les sites avec/sans groupe électrogène
    (seuls ceux avec GE peuvent avoir une conso fuel) ; omis, retourne tous
    les sites du pays.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Q, Sum

        from django.utils import timezone

        from fuel_tracking.models import (
            FuelCphGeParameter,
            FuelConsommationMonthly,
            FuelConsommationSyncRun,
            FuelEnocSyncRun,
        )

        # Statut du fichier de référence CPH (FuelCphGeParameter) — pas une
        # connectivité live comme Snowflake/ENOC, mais l'UI a besoin de savoir
        # si le fichier a été chargé pour expliquer pourquoi conso_estimee_cph_l
        # est vide partout (MISSING_PARAMETER) tant qu'il ne l'est pas.
        today = timezone.now().date()
        cph_params_qs = FuelCphGeParameter.objects.all()
        cph_parameters = {
            "sites_configures": cph_params_qs.filter(
                valid_from__lte=today
            ).exclude(valid_to__lt=today).values("site_id").distinct().count(),
            "dernier_import": cph_params_qs.order_by("-updated_at").values_list("updated_at", flat=True).first(),
        }

        available_months = list(
            FuelConsommationMonthly.objects.order_by("-month_year")
            .values_list("month_year", flat=True)
            .distinct()
        )

        last_sf_run = FuelConsommationSyncRun.objects.order_by("-started_at").first()
        last_enoc_run = FuelEnocSyncRun.objects.order_by("-started_at").first()
        sources = {
            "snowflake": {
                "connected": bool(last_sf_run and last_sf_run.status == FuelConsommationSyncRun.Status.SUCCESS),
                "last_status": last_sf_run.status if last_sf_run else None,
                "last_run_at": (last_sf_run.finished_at or last_sf_run.started_at) if last_sf_run else None,
                "error": last_sf_run.error_message if last_sf_run else None,
            },
            "enoc": {
                "connected": bool(last_enoc_run and last_enoc_run.status == FuelEnocSyncRun.Status.SUCCESS and last_enoc_run.rows_fetched > 0),
                "last_status": last_enoc_run.status if last_enoc_run else None,
                "last_run_at": (last_enoc_run.finished_at or last_enoc_run.started_at) if last_enoc_run else None,
                "error": last_enoc_run.error_message if last_enoc_run else None,
            },
        }

        month = request.query_params.get("month")
        if not month:
            if not available_months:
                return Response({"month_year": None, "data": [], "pagination": None, "available_months": [], "kpis": None, "sources": sources, "cph_parameters": cph_parameters})
            month = available_months[0]

        qs = FuelConsommationMonthly.objects.filter(month_year=month).order_by("site_id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        country = (request.query_params.get("country") or "").strip()
        if country:
            qs = qs.filter(country=country)

        # Répartition avec/sans GE calculée AVANT le filtre has_genset lui-même,
        # pour que le sélecteur du frontend puisse toujours afficher les 2
        # effectifs (ex: "Avec GE (373)" / "Sans GE (2949)"), qu'un filtre soit
        # actif ou non.
        ge_counts = qs.aggregate(
            sites_avec_ge=Count("id", filter=Q(has_genset=True)),
            sites_sans_ge=Count("id", filter=Q(has_genset=False)),
            sites_ge_enoc_only=Count("id", filter=Q(has_genset_enoc=True, has_genset_snowflake=False)),
        )

        has_genset_param = (request.query_params.get("has_genset") or "").strip().lower()
        if has_genset_param in ("true", "1"):
            qs = qs.filter(has_genset=True)
        elif has_genset_param in ("false", "0"):
            qs = qs.filter(has_genset=False)

        agg = qs.aggregate(
            total_sites=Count("id"),
            sites_avec_conso=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
            sites_avec_estimation=Count("id", filter=Q(conso_estimee_snowflake_l__isnull=False) | Q(conso_estimee_enoc_l__isnull=False)),
            total_conso_snowflake_l=Sum("conso_snowflake_l"),
            total_enoc_qte_ajoutee_l=Sum("enoc_qte_ajoutee_l"),
            total_enoc_nb_demandes=Sum("enoc_nb_demandes"),
        )
        kpis = {
            "total_sites": agg["total_sites"],
            "sites_avec_ge": ge_counts["sites_avec_ge"],
            "sites_sans_ge": ge_counts["sites_sans_ge"],
            "sites_ge_enoc_only": ge_counts["sites_ge_enoc_only"],
            "sites_avec_conso": agg["sites_avec_conso"],
            "sites_avec_estimation": agg["sites_avec_estimation"],
            "total_conso_snowflake_l": float(agg["total_conso_snowflake_l"] or 0),
            "total_enoc_qte_ajoutee_l": float(agg["total_enoc_qte_ajoutee_l"] or 0),
            "total_enoc_nb_demandes": agg["total_enoc_nb_demandes"] or 0,
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        total = agg["total_sites"]
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        rows = qs[start:start + limit]

        def serialize(row):
            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typology": row.typology,
                "site_type": row.site_type,
                "dg_count": row.dg_count,
                "power_supply": row.power_supply,
                "has_genset": row.has_genset,
                "has_genset_snowflake": row.has_genset_snowflake,
                "has_genset_enoc": row.has_genset_enoc,
                "nb_ge_enoc": row.nb_ge_enoc,
                "conso_snowflake_l": float(row.conso_snowflake_l) if row.conso_snowflake_l is not None else None,
                "nb_jours_data": row.nb_jours_data,
                "conso_estimee_snowflake_l": float(row.conso_estimee_snowflake_l) if row.conso_estimee_snowflake_l is not None else None,
                "conso_estimee_snowflake_nb_releves": row.conso_estimee_snowflake_nb_releves,
                "conso_estimee_enoc_l": float(row.conso_estimee_enoc_l) if row.conso_estimee_enoc_l is not None else None,
                "conso_estimee_nb_releves": row.conso_estimee_nb_releves,
                "conso_specifique_moy_l_kwh": float(row.conso_specifique_moy_l_kwh) if row.conso_specifique_moy_l_kwh is not None else None,
                "ge_prod_kwh": float(row.ge_prod_kwh) if row.ge_prod_kwh is not None else None,
                "sensor_status": row.sensor_status,
                # Colonnes qualité VW_FUEL_REPORT — audit (spec 2026-08).
                "quality_status": row.quality_status,
                "raw_point_count": row.raw_point_count,
                "valid_point_count": row.valid_point_count,
                "isolated_spike_count": row.isolated_spike_count,
                "over_capacity_point_count": row.over_capacity_point_count,
                "refill_detected": row.refill_detected,
                "estimated_refill_volume_l": float(row.estimated_refill_volume_l) if row.estimated_refill_volume_l is not None else None,
                "enoc_qte_demandee_l": float(row.enoc_qte_demandee_l),
                "enoc_qte_validee_l": float(row.enoc_qte_validee_l),
                "enoc_qte_ajoutee_l": float(row.enoc_qte_ajoutee_l),
                "enoc_nb_demandes": row.enoc_nb_demandes,
                "ecart_conso_vs_enoc_l": float(row.ecart_conso_vs_enoc_l) if row.ecart_conso_vs_enoc_l is not None else None,
                # Estimation CPH (télémétrie GFMS_DATA_TRACKER_NC) — troisième
                # source, indépendante des 2 ci-dessus, pour les GE sans
                # capteur de cuve fiable. Voir sync_fuel_cph/FuelCphGeDaily.
                "conso_estimee_cph_l": float(row.conso_estimee_cph_l) if row.conso_estimee_cph_l is not None else None,
                "cph_l_per_h_moy": float(row.cph_l_per_h_moy) if row.cph_l_per_h_moy is not None else None,
                "cph_nb_jours_ok": row.cph_nb_jours_ok,
                "cph_nb_jours_calcules": row.cph_nb_jours_calcules,
                "cph_calculation_status": row.cph_calculation_status,
                "cph_status_breakdown": row.cph_status_breakdown,
                "cph_runtime_h_total": float(row.cph_runtime_h_total) if row.cph_runtime_h_total is not None else None,
                "cph_runtime_source": row.cph_runtime_source,
                "cph_ge_type": row.cph_ge_type,
            }

        return Response({
            "month_year": month,
            "data": [serialize(r) for r in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
            "available_months": available_months,
            "sources": sources,
            "kpis": kpis,
            "cph_parameters": cph_parameters,
        })


class FuelConsommationDashboardView(APIView):
    """
    GET /api/fuel-tracking/consommation/dashboard/?month=YYYY-MM&from_month=YYYY-MM&to_month=YYYY-MM

    Alimente l'onglet Dashboard. Périmètre restreint aux sites AVEC GE
    (has_genset=True) : seuls eux peuvent avoir une consommation fuel,
    inclure les sites sans GE ne ferait que diluer les totaux avec des
    lignes structurellement vides.

    Portée de `months`/`monthly`/`top_sites` (priorité dans cet ordre,
    spec 2026-08) :
      1. `from_month` + `to_month` fournis ensemble → cette plage exacte.
      2. sinon `month` seul (même sélecteur que l'onglet Suivis
         Consommations) fourni et explicitement choisi par l'utilisateur
         → ce seul mois.
      3. sinon (aucun paramètre) → les 3 derniers mois disponibles par
         défaut (comparatif), jamais tout l'historique.
    """
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _month_stats(qs, month_year):
        from django.db.models import Count, Q, Sum

        agg = qs.aggregate(
            nb_sites_ge=Count("id"),
            nb_sites_avec_conso=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
            nb_sites_monitored=Count("id", filter=Q(sensor_status="MONITORED")),
            total_conso_snowflake_l=Sum("conso_snowflake_l"),
            total_enoc_qte_ajoutee_l=Sum("enoc_qte_ajoutee_l"),
            total_enoc_nb_demandes=Sum("enoc_nb_demandes"),
            nb_sites_enoc_ajoutee=Count("id", filter=Q(enoc_qte_ajoutee_l__gt=0)),
            # Ratio pondéré (pas une moyenne de ratios) — seulement les lignes
            # où les 2 valeurs existent, même principe que le calcul par site
            # (cf. fuel_consommation_snowflake.py).
            specif_num=Sum("conso_snowflake_l", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
            specif_den=Sum("ge_prod_kwh", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
        )
        specif_num = agg["specif_num"]
        specif_den = agg["specif_den"]
        conso_specifique = (
            float(specif_num) / float(specif_den)
            if specif_num is not None and specif_den not in (None, 0)
            else None
        )
        return {
            "month_year": month_year,
            "nb_sites_ge": agg["nb_sites_ge"],
            "nb_sites_avec_conso": agg["nb_sites_avec_conso"],
            "nb_sites_monitored": agg["nb_sites_monitored"],
            "total_conso_snowflake_l": float(agg["total_conso_snowflake_l"] or 0),
            "total_enoc_qte_ajoutee_l": float(agg["total_enoc_qte_ajoutee_l"] or 0),
            "total_enoc_nb_demandes": agg["total_enoc_nb_demandes"] or 0,
            "nb_sites_enoc_ajoutee": agg["nb_sites_enoc_ajoutee"],
            "conso_specifique_moy_l_kwh": conso_specifique,
        }

    @staticmethod
    def _top_sites(qs, limit=10):
        from django.db.models import Count, Sum

        rows = (
            qs.exclude(conso_snowflake_l__isnull=True)
            .values("site_id", "site_name")
            .annotate(
                total_conso_l=Sum("conso_snowflake_l"),
                nb_mois_avec_conso=Count("id"),
            )
            .order_by("-total_conso_l")[:limit]
        )
        return [
            {
                "site_id": r["site_id"],
                "site_name": r["site_name"],
                "total_conso_l": float(r["total_conso_l"] or 0),
                "nb_mois_avec_conso": r["nb_mois_avec_conso"],
            }
            for r in rows
        ]

    def get(self, request):
        from fuel_tracking.models import FuelConsommationMonthly

        base_qs = FuelConsommationMonthly.objects.filter(has_genset=True)
        all_months = list(
            base_qs.order_by("month_year").values_list("month_year", flat=True).distinct()
        )
        total_ge_sites = base_qs.filter(month_year=all_months[-1]).count() if all_months else 0

        requested_month = (request.query_params.get("month") or "").strip()
        from_month = (request.query_params.get("from_month") or "").strip()
        to_month = (request.query_params.get("to_month") or "").strip()

        if from_month and to_month:
            trend_months = [m for m in all_months if from_month <= m <= to_month]
        elif requested_month:
            trend_months = [m for m in all_months if m == requested_month]
        else:
            trend_months = all_months[-3:]

        trend_qs = base_qs.filter(month_year__in=trend_months)

        monthly = [
            self._month_stats(trend_qs.filter(month_year=my), my)
            for my in trend_months
        ]
        top_sites = self._top_sites(trend_qs)

        return Response({
            "months": trend_months,
            "monthly": monthly,
            "top_sites": top_sites,
            "total_ge_sites": total_ge_sites,
            "available_months": all_months,
        })


class FuelStockListView(APIView):
    """
    GET /api/fuel-tracking/stock/?search=&has_genset=&page=&limit=

    Stock carburant ACTUEL par site (FuelStockSnapshot) — jointure Snowflake
    (VW_FUEL_REPORT) + ENOC (fuel_level_readings), voir sync_fuel_stock. Pas
    de notion de mois : une seule ligne par site, remplacée à chaque sync.
    `has_genset=true|false` filtre les sites avec/sans GE (seuls les sites
    avec GE consomment/stockent du fuel) ; omis, retourne tous les sites.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Q

        from fuel_tracking.models import FuelStockSnapshot, FuelStockSyncRun, FuelEnocSyncRun

        last_sf_run = FuelStockSyncRun.objects.order_by("-started_at").first()
        last_enoc_run = FuelEnocSyncRun.objects.order_by("-started_at").first()
        sources = {
            "snowflake": {
                "connected": bool(last_sf_run and last_sf_run.status == FuelStockSyncRun.Status.SUCCESS),
                "last_status": last_sf_run.status if last_sf_run else None,
                "last_run_at": (last_sf_run.finished_at or last_sf_run.started_at) if last_sf_run else None,
                "error": last_sf_run.error_message if last_sf_run else None,
            },
            "enoc": {
                "connected": bool(last_enoc_run and last_enoc_run.status == FuelEnocSyncRun.Status.SUCCESS and last_enoc_run.rows_fetched > 0),
                "last_status": last_enoc_run.status if last_enoc_run else None,
                "last_run_at": (last_enoc_run.finished_at or last_enoc_run.started_at) if last_enoc_run else None,
                "error": last_enoc_run.error_message if last_enoc_run else None,
            },
        }

        qs = FuelStockSnapshot.objects.all().order_by("site_id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        ge_counts = qs.aggregate(
            sites_avec_ge=Count("id", filter=Q(has_genset=True)),
            sites_sans_ge=Count("id", filter=Q(has_genset=False)),
        )

        has_genset_param = (request.query_params.get("has_genset") or "").strip().lower()
        if has_genset_param in ("true", "1"):
            qs = qs.filter(has_genset=True)
        elif has_genset_param in ("false", "0"):
            qs = qs.filter(has_genset=False)

        agg = qs.aggregate(
            total_sites=Count("id"),
            sites_avec_stock_snowflake=Count("id", filter=Q(stock_snowflake_l__isnull=False)),
            sites_avec_stock_enoc=Count("id", filter=Q(stock_enoc_l__isnull=False)),
        )
        kpis = {
            "total_sites": agg["total_sites"],
            "sites_avec_ge": ge_counts["sites_avec_ge"],
            "sites_sans_ge": ge_counts["sites_sans_ge"],
            "sites_avec_stock_snowflake": agg["sites_avec_stock_snowflake"],
            "sites_avec_stock_enoc": agg["sites_avec_stock_enoc"],
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        total = agg["total_sites"]
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        rows = qs[start:start + limit]

        def serialize(row):
            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typology": row.typology,
                "site_type": row.site_type,
                "dg_count": row.dg_count,
                "power_supply": row.power_supply,
                "has_genset": row.has_genset,
                "has_genset_snowflake": row.has_genset_snowflake,
                "has_genset_enoc": row.has_genset_enoc,
                "nb_ge_enoc": row.nb_ge_enoc,
                "stock_snowflake_l": float(row.stock_snowflake_l) if row.stock_snowflake_l is not None else None,
                "capacity_snowflake_l": float(row.capacity_snowflake_l) if row.capacity_snowflake_l is not None else None,
                "stock_snowflake_pct": float(row.stock_snowflake_pct) if row.stock_snowflake_pct is not None else None,
                "stock_snowflake_date": row.stock_snowflake_date,
                "quality_status": row.quality_status,
                "stock_enoc_l": float(row.stock_enoc_l) if row.stock_enoc_l is not None else None,
                "stock_enoc_date": row.stock_enoc_date,
            }

        return Response({
            "data": [serialize(r) for r in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
            "sources": sources,
            "kpis": kpis,
        })
