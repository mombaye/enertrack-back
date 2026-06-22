from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    ImportBatchViewSet,
    SonatelInvoiceViewSet,
    MonthlySynthesisViewSet,
    TariffRateViewSet,
    ContractSiteLinkViewSet,
    ContractMonthViewSet,  # ✅ ADD
    SonatelBillingStatsAPIView,
    ImpactedSitesAPIView,
    FNPSitesAPIView
)

from billing.views_compute import SonatelBillingComputeView

#from billing.views_consumptions import ConsumptionLineViewSet
#from billing.views_preinvoices import PreInvoiceViewSet




router = DefaultRouter()
router.register(r"sonatel-billing/batches", ImportBatchViewSet, basename="sb-batches")
router.register(r"sonatel-billing/records", SonatelInvoiceViewSet, basename="sb-records")
router.register(r"sonatel-billing/monthly", MonthlySynthesisViewSet, basename="sb-monthly")
router.register(r"sonatel-billing/tariff-rates", TariffRateViewSet, basename="sb-tariff-rates")
router.register(r"sonatel-billing/contract-site-links", ContractSiteLinkViewSet, basename="sb-contract-site-links")
router.register(r"sonatel-billing/contract-months", ContractMonthViewSet, basename="sb-contract-months")  # ✅ ADD


#router.register(r"sonatel-billing/consumptions", ConsumptionLineViewSet, basename="consumptions")
#router.register(r"sonatel-billing/preinvoices", PreInvoiceViewSet, basename="preinvoices")

urlpatterns = [
    path("", include(router.urls)),
    path("sonatel-billing/compute/", SonatelBillingComputeView.as_view(), name="sb-compute"),
    path("sonatel-billing/stats/", SonatelBillingStatsAPIView.as_view()),
    path("billing/impacted-sites/", ImpactedSitesAPIView.as_view(), name="impacted-sites"),
    path("sonatel-billing/fnp/", FNPSitesAPIView.as_view(), name="sb-fnp"),
    
]
