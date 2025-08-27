# pwm/admin.py
from django.contrib import admin
from .models import PwmReport

@admin.register(PwmReport)
class PwmReportAdmin(admin.ModelAdmin):
    list_display = ("site", "period_start", "period_end", "total_pwm_avg_w", "grid_availability_pct")
    list_filter = ("country", "period_start")
    search_fields = ("site__site_id", "site__site_name", "source_filename")
