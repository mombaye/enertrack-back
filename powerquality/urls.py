from rest_framework.routers import DefaultRouter
from powerquality.views import PQReportViewSet

router = DefaultRouter()
router.register(r"pq", PQReportViewSet, basename="pq")
urlpatterns = router.urls
