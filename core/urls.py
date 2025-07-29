from django.urls import path, include
from .views import SiteImportView, ping, protected_ping, SiteViewSet
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'sites', SiteViewSet)

urlpatterns = [
    path("ping/", ping),
    path("secure-ping/", protected_ping),
    path("import/", SiteImportView.as_view(), name='site-import'),
    path("", include(router.urls)),
]
