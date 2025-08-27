# pwm/serializers.py
from rest_framework import serializers
from energy.serializers import CountrySerializer, SiteSerializer
from .models import PwmReport

class PwmReportSerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)
    site = SiteSerializer(read_only=True)

    class Meta:
        model = PwmReport
        fields = "__all__"
