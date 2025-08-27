# sonatel_billing/urls.py
from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ImportBatchViewSet, SonatelInvoiceViewSet, MonthlySynthesisViewSet

router = DefaultRouter()
router.register(r"sonatel-billing/batches", ImportBatchViewSet, basename="sb-batches")
router.register(r"sonatel-billing/records", SonatelInvoiceViewSet, basename="sb-records")
router.register(r"sonatel-billing/monthly", MonthlySynthesisViewSet, basename="sb-monthly")

urlpatterns = [path("", include(router.urls))]
