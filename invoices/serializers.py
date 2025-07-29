from rest_framework import serializers
from .models import Facture

class FactureSerializer(serializers.ModelSerializer):
    site_name = serializers.CharField(source='site.name', read_only=True)
    site_id = serializers.CharField(source='site.site_id', read_only=True)

    class Meta:
        model = Facture
        fields = '__all__'
