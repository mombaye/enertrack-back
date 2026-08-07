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

        from fuel_tracking.models import (
            FuelConsommationMonthly,
            FuelConsommationSyncRun,
            FuelEnocSyncRun,
        )

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
                return Response({"month_year": None, "data": [], "pagination": None, "available_months": [], "kpis": None, "sources": sources})
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
                "sensor_status": row.sensor_status,
                "enoc_qte_demandee_l": float(row.enoc_qte_demandee_l),
                "enoc_qte_validee_l": float(row.enoc_qte_validee_l),
                "enoc_qte_ajoutee_l": float(row.enoc_qte_ajoutee_l),
                "enoc_nb_demandes": row.enoc_nb_demandes,
                "ecart_conso_vs_enoc_l": float(row.ecart_conso_vs_enoc_l) if row.ecart_conso_vs_enoc_l is not None else None,
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
        })
