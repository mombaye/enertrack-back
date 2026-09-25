from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EstimationBatchViewSet,
    EstimationResultViewSet,
    EstimationHistoryImportView,
    ExternalEstimationImportView,
    EstimationCompareView,
)

router = DefaultRouter()
router.register("batches", EstimationBatchViewSet, basename="estimation-batch")
router.register("results", EstimationResultViewSet, basename="estimation-result")

urlpatterns = [
    path("", include(router.urls)),
    path("history/import/",  EstimationHistoryImportView.as_view(),  name="estimation-history-import"),
    path("external/import/", ExternalEstimationImportView.as_view(), name="estimation-external-import"),
    path("compare/",         EstimationCompareView.as_view(),        name="estimation-compare"),
]