# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import (
    FuelCommandeSyntheseImportView,
    FuelCommandeSyntheseView,
    FuelSuiviCommandeListView,
)

urlpatterns = [
    path("commande-synthese/", FuelCommandeSyntheseView.as_view(), name="fuel-commande-synthese"),
    path("commande-synthese/import/", FuelCommandeSyntheseImportView.as_view(), name="fuel-commande-synthese-import"),
    path("suivi-commande/", FuelSuiviCommandeListView.as_view(), name="fuel-suivi-commande"),
]
