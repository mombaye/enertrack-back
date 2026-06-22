# optimization/serializers.py

from rest_framework import serializers
from .models import OptimizationBatch, OptimizationResult


class OptimizationBatchSerializer(serializers.ModelSerializer):
    launched_by_name = serializers.SerializerMethodField()

    class Meta:
        model = OptimizationBatch
        fields = "__all__"

    def get_launched_by_name(self, obj):
        if not obj.launched_by:
            return None
        return getattr(obj.launched_by, "username", None) or str(obj.launched_by)


class OptimizationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = OptimizationResult
        fields = "__all__"