from django.urls import path, include
from .views import SiteImportView, ping, protected_ping, SiteViewSet, GridTargetRuleViewSet, GridTargetRuleImportView
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r'sites', SiteViewSet)
router.register(r"grid-target-rules", GridTargetRuleViewSet, basename="grid-target-rules")

urlpatterns = [
    path("ping/", ping),
    path("secure-ping/", protected_ping),
    path("import/", SiteImportView.as_view(), name='site-import'),
    path("grid-target-rules/import/", GridTargetRuleImportView.as_view(), name="grid-target-rule-import"),

    path("", include(router.urls)),
]
