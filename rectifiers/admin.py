# rectifiers/admin.py
from django.contrib import admin
from .models import RectifierReading

@admin.register(RectifierReading)
class RectifierReadingAdmin(admin.ModelAdmin):
    list_display = ("site", "param_name", "param_value", "measure", "measured_at", "source_filename")
    list_filter  = ("param_name", "measure", "site__country", "measured_at")
    search_fields = ("site__site_id", "site__site_name", "param_name", "source_filename")
