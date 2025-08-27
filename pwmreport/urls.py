# pwm/urls.py
from rest_framework.routers import DefaultRouter
from .views import PwmReportViewSet

router = DefaultRouter()
router.register(r"pwm", PwmReportViewSet, basename="pwm")
urlpatterns = router.urls
