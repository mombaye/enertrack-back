from django.contrib import admin

from fuel_tracking.models import (
    CphCurve,
    CphInventoryMapping,
    FuelObservationImport,
    FuelRapprochementThreshold,
)


@admin.register(CphCurve)
class CphCurveAdmin(admin.ModelAdmin):
    list_display = ("curve_id", "manufacturer", "model", "variant", "prp_kva", "prp_kw", "power_factor", "status", "business_approved")
    list_filter = ("status", "business_approved", "manufacturer")
    search_fields = ("curve_id", "manufacturer", "model", "model_key")
    # Seul le cos φ (configurable par type GE) est modifiable ici ; l'activation
    # métier passe par l'API (traçabilité utilisateur + commentaire obligatoire).
    readonly_fields = [f.name for f in CphCurve._meta.fields if f.name not in ("id", "power_factor")]


@admin.register(CphInventoryMapping)
class CphInventoryMappingAdmin(admin.ModelAdmin):
    list_display = ("inventory_label", "inventory_kva", "site_count", "abaque_status", "validated_curve", "validated_by", "validated_at")
    list_filter = ("abaque_status",)
    search_fields = ("inventory_label",)
    readonly_fields = [f.name for f in CphInventoryMapping._meta.fields if f.name != "id"]


@admin.register(FuelObservationImport)
class FuelObservationImportAdmin(admin.ModelAdmin):
    list_display = ("file_name", "uploaded_by", "uploaded_at", "rule_version", "rows_imported", "rows_rejected")
    readonly_fields = [f.name for f in FuelObservationImport._meta.fields if f.name != "id"]


admin.site.register(FuelRapprochementThreshold)
