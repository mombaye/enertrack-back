# certification/admin.py

from django.contrib import admin
from .models import CertificationBatch, CertificationResult, EfmsConnectionLog


@admin.register(CertificationBatch)
class CertificationBatchAdmin(admin.ModelAdmin):
    list_display  = ["id", "echeance_label", "status", "total", "certified_fms", "certified_senelec", "needs_review", "unknown_contract", "launched_at"]
    list_filter   = ["status", "echeance_year", "echeance_month"]
    readonly_fields = ["launched_at", "finished_at", "celery_task_id", "total", "certified_fms", "certified_senelec", "needs_review", "unknown_contract", "fms_unavailable"]
    ordering      = ["-launched_at"]


@admin.register(CertificationResult)
class CertificationResultAdmin(admin.ModelAdmin):
    list_display  = ["id", "invoice", "site", "status", "certified_by_rule", "ratio_fms_periode", "ratio_fms_30j", "ratio_histo_3mois", "computed_at"]
    list_filter   = ["status", "certified_by_rule", "fms_available"]
    search_fields = ["invoice__numero_facture", "invoice__numero_compte_contrat", "site__site_id"]
    readonly_fields = ["computed_at"]
    ordering      = ["-computed_at"]


@admin.register(EfmsConnectionLog)
class EfmsConnectionLogAdmin(admin.ModelAdmin):
    list_display = ["id", "status", "host", "duration_ms", "attempted_at", "cert_batch"]
    list_filter  = ["status"]
    readonly_fields = ["attempted_at"]
    ordering     = ["-attempted_at"]