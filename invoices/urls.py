from rest_framework.routers import DefaultRouter
from .views import FactureImportView, FactureViewSet
from django.urls import path, include

router = DefaultRouter()
router.register(r'', FactureViewSet)

urlpatterns = [
    # On place l'import AVANT le router !
    path('import/', FactureImportView.as_view(), name='facture-import'),
    path('', include(router.urls)),
]

