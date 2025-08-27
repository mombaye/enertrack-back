# energy/urls.py

from rest_framework.routers import DefaultRouter
from .views import EnergyStatViewSet, SiteEnergyViewSet

router = DefaultRouter()
router.register(r"energy", EnergyStatViewSet, basename="energy")
router.register(r"site-energy", SiteEnergyViewSet, basename="site-energy")

urlpatterns = router.urls
