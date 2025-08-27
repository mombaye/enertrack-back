# rectifiers/serializers.py
from rest_framework import serializers
from energy.models import Country, Site
from .models import RectifierReading


class CountryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ["id", "name"]


class SiteRefSerializer(serializers.ModelSerializer):
    country = CountryRefSerializer(read_only=True)

    class Meta:
        model = Site
        fields = ["id", "site_id", "site_name", "country"]


class RectifierReadingSerializer(serializers.ModelSerializer):
    site = SiteRefSerializer(read_only=True)

    class Meta:
        model = RectifierReading
        fields = [
            "id", "country", "site",
            "param_name", "param_value", "measure",
            "measured_at", "source_filename", "imported_at",
        ]
