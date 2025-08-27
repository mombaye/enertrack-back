# energy/admin.py (ajoute ces lignes si pas déjà faits)
from django.contrib import admin
from .models import Country, EnergyMonthlyStat, Site, SiteEnergyMonthlyStat

@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    search_fields = ("name",)

@admin.register(EnergyMonthlyStat)
class EnergyMonthlyStatAdmin(admin.ModelAdmin):
    list_display = ("country", "year", "month", "sites_integrated", "sites_monitored")
    list_filter = ("country", "year", "month")
    search_fields = ("country__name",)

@admin.register(Site)
class SiteAdmin(admin.ModelAdmin):
    list_display = ("site_id", "site_name", "country")
    list_filter = ("country",)
    search_fields = ("site_id", "site_name")

@admin.register(SiteEnergyMonthlyStat)
class SiteEnergyMonthlyStatAdmin(admin.ModelAdmin):
    list_display = ("site", "year", "month", "grid_status", "dg_status", "solar_status",
                    "grid_energy_kwh", "solar_energy_kwh", "telecom_load_kwh",
                    "grid_energy_pct", "rer_pct",
                    "router_availability_pct", "pwm_availability_pct", "pwc_availability_pct")
    list_filter = ("site__country", "year", "month", "grid_status", "dg_status", "solar_status")
    search_fields = ("site__site_id", "site__site_name", "source_filename")
