# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import (
    FuelCommandeEstimationView,
    FuelCommandeView,
    FuelConsommationDashboardView,
    FuelConsommationExportAnomaliesView,
    FuelConsommationExportControleView,
    FuelConsommationListView,
    FuelStockListView,
)

urlpatterns = [
    path("consommation/dashboard/", FuelConsommationDashboardView.as_view(), name="fuel-consommation-dashboard"),
    path("consommation/export/controle/", FuelConsommationExportControleView.as_view(), name="fuel-consommation-export-controle"),
    path("consommation/export/anomalies/", FuelConsommationExportAnomaliesView.as_view(), name="fuel-consommation-export-anomalies"),
    path("consommation/", FuelConsommationListView.as_view(), name="fuel-consommation"),
    path("stock/", FuelStockListView.as_view(), name="fuel-stock"),
    path("commandes/estimation/", FuelCommandeEstimationView.as_view(), name="fuel-commandes-estimation"),
    path("commandes/", FuelCommandeView.as_view(), name="fuel-commandes"),
]
