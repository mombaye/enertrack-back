# users/serializers.py

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import CustomUser


# ─────────────────────────────────────────────────────────────────────────────
# JWT — token enrichi
# ─────────────────────────────────────────────────────────────────────────────

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"]     = user.role
        token["pays"]     = user.pays
        token["username"] = user.username
        token["email"]    = user.email
        return token


# ─────────────────────────────────────────────────────────────────────────────
# User — lecture (liste + détail)
# ─────────────────────────────────────────────────────────────────────────────

class UserSerializer(serializers.ModelSerializer):
    """Lecture seule — pas de mot de passe exposé."""

    class Meta:
        model  = CustomUser
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "pays",
            "is_active",
            "date_joined",
            "last_login",
        ]
        read_only_fields = ["id", "date_joined", "last_login"]


# ─────────────────────────────────────────────────────────────────────────────
# User — création (avec mot de passe obligatoire)
# ─────────────────────────────────────────────────────────────────────────────

class UserCreateSerializer(serializers.ModelSerializer):
    password  = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, required=True, label="Confirmation mot de passe")

    class Meta:
        model  = CustomUser
        fields = [
            "username", "email",
            "first_name", "last_name",
            "role", "pays",
            "is_active",
            "password", "password2",
        ]

    def validate(self, data):
        if data["password"] != data["password2"]:
            raise serializers.ValidationError({"password2": "Les mots de passe ne correspondent pas."})
        return data

    def create(self, validated_data):
        validated_data.pop("password2")
        password = validated_data.pop("password")
        user = CustomUser(**validated_data)
        user.set_password(password)
        user.save()
        return user


# ─────────────────────────────────────────────────────────────────────────────
# User — mise à jour (mot de passe optionnel)
# ─────────────────────────────────────────────────────────────────────────────

class UserUpdateSerializer(serializers.ModelSerializer):
    password  = serializers.CharField(write_only=True, required=False, allow_blank=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, required=False, allow_blank=True, label="Confirmation mot de passe")

    class Meta:
        model  = CustomUser
        fields = [
            "username", "email",
            "first_name", "last_name",
            "role", "pays",
            "is_active",
            "password", "password2",
        ]

    def validate(self, data):
        p1 = data.get("password", "")
        p2 = data.get("password2", "")
        if p1 or p2:
            if p1 != p2:
                raise serializers.ValidationError({"password2": "Les mots de passe ne correspondent pas."})
        return data

    def update(self, instance, validated_data):
        validated_data.pop("password2", None)
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance