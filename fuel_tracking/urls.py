# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import FuelCommandeView, FuelConsommationDashboardView, FuelConsommationListView, FuelStockListView

urlpatterns = [
    path("consommation/dashboard/", FuelConsommationDashboardView.as_view(), name="fuel-consommation-dashboard"),
    path("consommation/", FuelConsommationListView.as_view(), name="fuel-consommation"),
    path("stock/", FuelStockListView.as_view(), name="fuel-stock"),
    path("commandes/", FuelCommandeView.as_view(), name="fuel-commandes"),
]
