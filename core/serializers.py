from rest_framework import serializers
from .models import Site, GridTargetRule


class SiteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Site
        fields = "__all__"


class GridTargetRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = GridTargetRule
        fields = "__all__"