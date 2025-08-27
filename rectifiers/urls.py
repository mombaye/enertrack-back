# rectifiers/urls.py
from rest_framework.routers import DefaultRouter
from .views import RectifierReadingViewSet

router = DefaultRouter()
router.register(r"rectifiers", RectifierReadingViewSet, basename="rectifiers")

urlpatterns = router.urls
