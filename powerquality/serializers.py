from rest_framework import serializers
from energy.models import Country, Site
from powerquality.models import PQReport


class CountryRefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ["id", "name"]


class SiteRefSerializer(serializers.ModelSerializer):
    country = CountryRefSerializer(read_only=True)

    class Meta:
        model = Site
        fields = ["id", "site_id", "site_name", "country"]


class PQReportSerializer(serializers.ModelSerializer):
    site = SiteRefSerializer(read_only=True)

    class Meta:
        model = PQReport
        fields = "__all__"
