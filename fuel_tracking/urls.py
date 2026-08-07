# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import FuelConsommationListView

urlpatterns = [
    path("consommation/", FuelConsommationListView.as_view(), name="fuel-consommation"),
]
