# users/urls.py

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from .views import CustomLoginView, MeView, UserViewSet

router = DefaultRouter()
router.register(r"users", UserViewSet, basename="users")

urlpatterns = [
    # Auth
    path("auth/login/", CustomLoginView.as_view(), name="login"),

    # Profil connecté
    path("users/me/", MeView.as_view(), name="users-me"),

    # CRUD admin
    path("", include(router.urls)),
]

# ─────────────────────────────────────────────────────────────────────────────
# À inclure dans le urls.py racine :
#
#   path("api/", include("users.urls")),
#
# Endpoints résultants :
#   POST   /api/auth/login/
#   GET    /api/users/me/
#   GET    /api/users/
#   POST   /api/users/
#   GET    /api/users/{id}/
#   PUT    /api/users/{id}/
#   PATCH  /api/users/{id}/
#   DELETE /api/users/{id}/
#   POST   /api/users/{id}/toggle-active/
# ─────────────────────────────────────────────────────────────────────────────