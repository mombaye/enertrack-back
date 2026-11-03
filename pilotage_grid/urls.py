from django.urls import path

from pilotage_grid import views as V

urlpatterns = [
    path("datasets/", V.DatasetListView.as_view(), name="pg-datasets"),
    path("datasets/<str:key>/", V.DatasetDetailView.as_view(), name="pg-dataset"),
    path("datasets/<str:key>/versions/", V.DatasetVersionsView.as_view(), name="pg-dataset-versions"),
    path("datasets/<str:key>/import/", V.DatasetImportView.as_view(), name="pg-dataset-import"),
    path("datasets/<str:key>/export/", V.DatasetExportView.as_view(), name="pg-dataset-export"),
    path("datasets/<str:key>/versions/<int:version>/restore/", V.DatasetRestoreView.as_view(), name="pg-dataset-restore"),
    path("export/", V.ExportAllView.as_view(), name="pg-export"),
    path("facets/", V.FacetsView.as_view(), name="pg-facets"),
    path("synthese/", V.SyntheseView.as_view(), name="pg-synthese"),
    path("paiements/", V.PaiementsView.as_view(), name="pg-paiements"),
    path("paiements/factures/", V.FacturesView.as_view(), name="pg-factures"),
]
