from rest_framework import serializers
from .models import EnergyMonthlyStat, Country
# energy/serializers.py

from rest_framework import serializers
from .models import Country, Site, SiteEnergyMonthlyStat, EnergyMonthlyStat

class CountrySerializer(serializers.ModelSerializer):
    class Meta:
        model = Country
        fields = ['id', 'name']

class EnergyMonthlyStatSerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)

    class Meta:
        model = EnergyMonthlyStat
        fields = [
            'id', 'country', 'year', 'month',
            'sites_integrated','sites_monitored',
            'grid_mwh','solar_mwh','generators_mwh','telecom_mwh',
            'grid_pct','rer_pct','generators_pct',
            'avg_telecom_load_mw',
            'source_filename','imported_at'
        ]







class SiteSerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)
    country_id = serializers.PrimaryKeyRelatedField(
        queryset=Country.objects.all(), source="country", write_only=True, required=True
    )

    class Meta:
        model = Site
        fields = ["id", "site_id", "site_name", "country", "country_id"]

class SiteEnergyMonthlyStatSerializer(serializers.ModelSerializer):
    site = SiteSerializer(read_only=True)
    site_id_fk = serializers.PrimaryKeyRelatedField(
        queryset=Site.objects.all(), source="site", write_only=True, required=True
    )

    class Meta:
        model = SiteEnergyMonthlyStat
        fields = [
            "id", "site", "site_id_fk", "year", "month",
            "grid_status", "dg_status", "solar_status",
            "grid_energy_kwh", "solar_energy_kwh", "telecom_load_kwh",
            "grid_energy_pct", "rer_pct",
            "router_availability_pct", "pwm_availability_pct", "pwc_availability_pct",
            "source_filename", "imported_at",
        ]


# (si tu n’as pas déjà le serializer global)
class EnergyMonthlyStatSerializer(serializers.ModelSerializer):
    country = CountrySerializer(read_only=True)

    class Meta:
        model = EnergyMonthlyStat
        fields = "__all__"
