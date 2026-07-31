# billing/serializers.py — PATCH payment_status
#
# Seul SonatelInvoiceSerializer est modifié :
#   + payment_status et payment_status_updated_at exposés en read_only_fields

from rest_framework import serializers
from .models import (
    ImportBatch, SonatelInvoice, MonthlySynthesis, ContractMonth,
    ContractSiteLink, TariffRate, ImportIssue
)


class SonatelInvoiceSerializer(serializers.ModelSerializer):
    site_code = serializers.CharField(source="site.site_id", read_only=True)
    site_name = serializers.CharField(source="site.name", read_only=True)

    class Meta:
        model = SonatelInvoice
        fields = "__all__"
        read_only_fields = (
            "id", "batch", "created_at", "updated_at",
            "last_seen_at", "last_seen_batch",
            "status_updated_at", "status_last_batch",
            # ✅ payment_status en lecture seule (mis à jour via import Excel)
             "payment_status", "payment_status_updated_at",
        )


from .models import ContractMonth

class ImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportBatch
        fields = [
            "id", "source_filename", "imported_at",
            "task_id", "task_status", "task_progress",
            "task_message", "task_meta", "task_updated_at",
        ]


class MonthlySynthesisSerializer(serializers.ModelSerializer):
    site_id   = serializers.CharField(source="source.site.site_id", read_only=True)
    site_name = serializers.CharField(source="source.site.name",    read_only=True)

    class Meta:
        model  = MonthlySynthesis
        fields = "__all__"


class ContractMonthSerializer(serializers.ModelSerializer):
    site_id   = serializers.CharField(read_only=True)
    site_name = serializers.CharField(read_only=True)

    class Meta:
        model  = ContractMonth
        fields = "__all__"


class TariffRateSerializer(serializers.ModelSerializer):
    class Meta:
        model  = TariffRate
        fields = [
            "id", "category", "energie_k1", "energie_k2", "prime_fixe",
            "date_debut", "date_fin",
            "created_at", "updated_at", "last_seen_at", "last_seen_batch",
        ]


class ContractSiteLinkSerializer(serializers.ModelSerializer):
    site_id = serializers.CharField(source="site.site_id", read_only=True)
    site_pk = serializers.IntegerField(source="site_id",   read_only=True)

    class Meta:
        model  = ContractSiteLink
        fields = [
            "id", "numero_compte_contrat",
            "site_pk", "site_id",
            "first_seen_at", "last_seen_at",
            "source_filename", "imported_by",
        ]


class ImportIssueSerializer(serializers.ModelSerializer):
    class Meta:
        model  = ImportIssue
        fields = "__all__"