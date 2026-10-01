# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import (
    FuelCommandeEstimationView,
    FuelCommandeView,
    FuelConsommationDashboardView,
    FuelConsommationListView,
    FuelStockListView,
)
from fuel_tracking.views_cph import (
    CphAbaqueImportView,
    CphCurveApproveView,
    CphCurveRevokeView,
    CphExportAnomaliesView,
    CphExportControleView,
    CphHealthView,
    CphMappingAutoMatchView,
    CphMappingUnvalidateView,
    CphMappingValidateView,
    CphObservationImportListView,
    CphObservationImportView,
    CphPeriodView,
    CphReferentielView,
    CphSiteDetailView,
)

urlpatterns = [
    path("consommation/dashboard/", FuelConsommationDashboardView.as_view(), name="fuel-consommation-dashboard"),
    path("consommation/", FuelConsommationListView.as_view(), name="fuel-consommation"),
    path("stock/", FuelStockListView.as_view(), name="fuel-stock"),
    path("commandes/estimation/", FuelCommandeEstimationView.as_view(), name="fuel-commandes-estimation"),
    path("commandes/", FuelCommandeView.as_view(), name="fuel-commandes"),

    path("cph/", CphPeriodView.as_view(), name="fuel-cph"),
    path("cph/health/", CphHealthView.as_view(), name="fuel-cph-health"),
    path("cph/sites/<str:site_id>/", CphSiteDetailView.as_view(), name="fuel-cph-site"),
    path("cph/export/controle/", CphExportControleView.as_view(), name="fuel-cph-export-controle"),
    path("cph/export/anomalies/", CphExportAnomaliesView.as_view(), name="fuel-cph-export-anomalies"),
    path("cph/observations/import/", CphObservationImportView.as_view(), name="fuel-cph-observations-import"),
    path("cph/observations/imports/", CphObservationImportListView.as_view(), name="fuel-cph-observations-imports"),
    path("cph/referentiel/", CphReferentielView.as_view(), name="fuel-cph-referentiel"),
    path("cph/abaque/import/", CphAbaqueImportView.as_view(), name="fuel-cph-abaque-import"),
    path("cph/mappings/auto-match/", CphMappingAutoMatchView.as_view(), name="fuel-cph-mapping-auto-match"),
    path("cph/mappings/<int:pk>/validate/", CphMappingValidateView.as_view(), name="fuel-cph-mapping-validate"),
    path("cph/mappings/<int:pk>/unvalidate/", CphMappingUnvalidateView.as_view(), name="fuel-cph-mapping-unvalidate"),
    path("cph/curves/<str:curve_id>/approve/", CphCurveApproveView.as_view(), name="fuel-cph-curve-approve"),
    path("cph/curves/<str:curve_id>/revoke/", CphCurveRevokeView.as_view(), name="fuel-cph-curve-revoke"),
]
