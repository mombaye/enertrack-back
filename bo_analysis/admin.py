from django.contrib import admin

from .models import BOAnalysis, BOAnalysisRequest, BOMarginSnapshot


@admin.register(BOMarginSnapshot)
class BOMarginSnapshotAdmin(admin.ModelAdmin):
    list_display = ("site_id_raw", "site_name_raw", "zone", "categorie_bo", "statut_marge", "imported_at")
    list_filter = ("zone", "categorie_bo", "statut_marge")
    search_fields = ("site_id_raw", "site_name_raw")


@admin.register(BOAnalysisRequest)
class BOAnalysisRequestAdmin(admin.ModelAdmin):
    list_display = ("site", "year", "month", "status", "requested_by", "assigned_bo", "requested_at")
    list_filter = ("status", "year", "month")
    search_fields = ("site__site_id", "site__name")


@admin.register(BOAnalysis)
class BOAnalysisAdmin(admin.ModelAdmin):
    list_display = ("request", "categorie_bo", "action_owner", "submitted_by", "submitted_at")
    list_filter = ("categorie_bo", "action_owner")
