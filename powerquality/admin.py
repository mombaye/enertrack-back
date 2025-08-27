from django.contrib import admin
from powerquality.models import PQReport

@admin.register(PQReport)
class PQReportAdmin(admin.ModelAdmin):
    list_display = ("site", "begin_period", "end_period", "mono_total_energy_kwh", "tri_total_energy_kwh", "source_filename")
    list_filter = ("site__country", "begin_period")
    search_fields = ("site__site_id", "site__site_name", "source_filename")
