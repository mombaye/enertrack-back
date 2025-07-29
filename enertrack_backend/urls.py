from django.contrib import admin
from django.urls import path, include
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from users.views import CustomLoginView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/core/', include('core.urls')),
    
    # Auth JWT
    path('api/auth/login/', CustomLoginView.as_view(), name='token_obtain_pair'),
    path('api/auth/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    #Core URLs
    path('api/core/', include('core.urls')),

    # Invoices URLs
    path('api/invoices/', include('invoices.urls')),

    
]
