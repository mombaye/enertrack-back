from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    EstimationBatchViewSet,
    EstimationResultViewSet,
    EstimationHistoryImportView,
)

router = DefaultRouter()
router.register("batches", EstimationBatchViewSet, basename="estimation-batch")
router.register("results", EstimationResultViewSet, basename="estimation-result")

urlpatterns = [
    path("", include(router.urls)),
    path("history/import/", EstimationHistoryImportView.as_view(), name="estimation-history-import"),
]