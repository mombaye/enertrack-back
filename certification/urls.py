# certification/urls.py

from django.urls import path, include
from rest_framework.routers import DefaultRouter

from .views import (
    CertificationBatchViewSet,
    CertificationResultViewSet,
    EfmsConnectionLogViewSet,
    EfmsHealthCheckView,
)

router = DefaultRouter()
router.register(r"batches",   CertificationBatchViewSet,  basename="cert-batches")
router.register(r"results",   CertificationResultViewSet, basename="cert-results")
router.register(r"efms-logs", EfmsConnectionLogViewSet,   basename="cert-efms-logs")

urlpatterns = [
    path("", include(router.urls)),
    path("efms-health/", EfmsHealthCheckView.as_view(), name="cert-efms-health"),
]

# Dans enertrack_backend/urls.py ajouter :
#   path("api/certification/", include("certification.urls")),
#
# Endpoints exposes :
#   POST  /api/certification/batches/launch/
#   GET   /api/certification/batches/
#   GET   /api/certification/batches/{id}/
#   GET   /api/certification/batches/{id}/status/
#   GET   /api/certification/results/
#   GET   /api/certification/results/{id}/
#   GET   /api/certification/efms-logs/
#   GET   /api/certification/efms-health/