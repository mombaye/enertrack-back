# optimization/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    OptimizationBatchViewSet,
    OptimizationResultViewSet,
    OptimizationContractsAPIView,
    OptimizationContractAnnualBaseAPIView,
    RunPowerOptimizationAPIView
)


router = DefaultRouter()
router.register(r"batches", OptimizationBatchViewSet, basename="optimization-batches")
router.register(r"results", OptimizationResultViewSet, basename="optimization-results")


urlpatterns = [
    path("", include(router.urls)),

    path(
        "contracts/",
        OptimizationContractsAPIView.as_view(),
        name="optimization-contracts",
    ),

    path(
        "contracts/<str:numero_compte_contrat>/annual-base/",
        OptimizationContractAnnualBaseAPIView.as_view(),
        name="optimization-contract-annual-base",
    ),
    path(
        "run-power/",
        RunPowerOptimizationAPIView.as_view(),
        name="optimization-run-power",
    ),
]