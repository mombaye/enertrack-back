# users/views.py

import logging

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .emails import send_account_created_email, send_account_deactivated_email
from .models import CustomUser
from .serializers import (
    CustomTokenObtainPairSerializer,
    UserCreateSerializer,
    UserSerializer,
    UserUpdateSerializer,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

class CustomLoginView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


# ─────────────────────────────────────────────────────────────────────────────
# Permission helper
# ─────────────────────────────────────────────────────────────────────────────

def _require_admin(user):
    if getattr(user, "role", None) != "admin":
        raise PermissionDenied("Réservé aux administrateurs.")


# ─────────────────────────────────────────────────────────────────────────────
# /api/users/me/  — profil du user connecté
# ─────────────────────────────────────────────────────────────────────────────

class MeView(APIView):
    """
    GET  /api/users/me/
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)


# ─────────────────────────────────────────────────────────────────────────────
# /api/users/  — CRUD complet (admin only)
# ─────────────────────────────────────────────────────────────────────────────

class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    """
    list     GET    /api/users/
    retrieve GET    /api/users/{id}/
    create   POST   /api/users/
    update   PUT    /api/users/{id}/
    partial  PATCH  /api/users/{id}/
    destroy  DELETE /api/users/{id}/
    toggle   POST   /api/users/{id}/toggle-active/
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        _require_admin(self.request.user)
        qs = CustomUser.objects.all().order_by("username")

        # Filtres
        role = self.request.query_params.get("role")
        pays = self.request.query_params.get("pays")
        q    = self.request.query_params.get("search")

        if role:
            qs = qs.filter(role=role)
        if pays:
            qs = qs.filter(pays=pays)
        if q:
            qs = qs.filter(username__icontains=q) | qs.filter(email__icontains=q)

        return qs

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action in ("update", "partial_update"):
            return UserUpdateSerializer
        return UserSerializer

    def perform_create(self, serializer):
        _require_admin(self.request.user)
        # Capturé avant .save() : UserCreateSerializer.create() consomme (pop) le
        # mot de passe du validated_data au moment de créer l'utilisateur.
        password = serializer.validated_data.get("password")
        user = serializer.save()
        logger.info(f"[USERS] Créé par {self.request.user}: {user.username} (role={user.role})")
        send_account_created_email(user, password)

    def perform_update(self, serializer):
        _require_admin(self.request.user)
        # Empêche de dégrader le seul admin
        instance = self.get_object()
        new_role = serializer.validated_data.get("role", instance.role)
        if instance.role == "admin" and new_role != "admin":
            admins_left = CustomUser.objects.filter(role="admin").exclude(pk=instance.pk).count()
            if admins_left == 0:
                from rest_framework.exceptions import ValidationError
                raise ValidationError({"role": "Impossible de retirer le rôle admin du dernier administrateur."})
        serializer.save()
        logger.info(f"[USERS] Modifié par {self.request.user}: {instance.username}")

    def perform_destroy(self, instance):
        _require_admin(self.request.user)
        # Empêche de supprimer le seul admin
        if instance.role == "admin":
            admins_left = CustomUser.objects.filter(role="admin").exclude(pk=instance.pk).count()
            if admins_left == 0:
                from rest_framework.exceptions import ValidationError
                raise ValidationError("Impossible de supprimer le dernier administrateur.")
        # Empêche l'auto-suppression
        if instance.pk == self.request.user.pk:
            from rest_framework.exceptions import ValidationError
            raise ValidationError("Vous ne pouvez pas supprimer votre propre compte.")
        logger.info(f"[USERS] Supprimé par {self.request.user}: {instance.username}")
        instance.delete()

    # ── POST /api/users/{id}/toggle-active/ ───────────────────────────────────
    @action(methods=["post"], detail=True, url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        """Active / désactive un compte utilisateur."""
        _require_admin(request.user)
        user = self.get_object()
        if user.pk == request.user.pk:
            return Response(
                {"detail": "Vous ne pouvez pas désactiver votre propre compte."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.is_active = not user.is_active
        user.save(update_fields=["is_active"])
        action_str = "activé" if user.is_active else "désactivé"
        logger.info(f"[USERS] {user.username} {action_str} par {request.user}")
        if not user.is_active:
            send_account_deactivated_email(user)
        return Response({
            "id":        user.id,
            "username":  user.username,
            "is_active": user.is_active,
            "detail":    f"Compte {action_str}.",
        })