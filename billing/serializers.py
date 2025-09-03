# sonatel_billing/serializers.py
from rest_framework import serializers
from .models import ImportBatch, SonatelInvoice, MonthlySynthesis

class ImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportBatch
        fields = ["id", "source_filename", "imported_at"]

class SonatelInvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = SonatelInvoice
        exclude = []

class MonthlySynthesisSerializer(serializers.ModelSerializer):
    class Meta:
        model = MonthlySynthesis
        exclude = []



class ContractMonthSerializer(serializers.ModelSerializer):
    class Meta:
        model = ContractMonth
        fields = "__all__"
