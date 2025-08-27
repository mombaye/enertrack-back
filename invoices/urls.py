from rest_framework.routers import DefaultRouter
from .views import FactureAsyncImportView, FactureImportView, FactureViewSet, ImportStatusView
from django.urls import path, include

router = DefaultRouter()
router.register(r'', FactureViewSet)

urlpatterns = [
    # On place l'import AVANT le router !
    path('import/', FactureImportView.as_view(), name='facture-import'),
     path('import_async/', FactureAsyncImportView.as_view(), name='facture-import-async'),
    path('import-status/<str:task_id>/', ImportStatusView.as_view(), name='facture-import-status'),
    path('', include(router.urls)),
]

