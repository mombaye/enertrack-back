# estimation/serializers.py

from rest_framework import serializers
from .models import EstimationBatch, EstimationResult


class EstimationBatchSerializer(serializers.ModelSerializer):
    label = serializers.ReadOnlyField()

    class Meta:
        model  = EstimationBatch
        fields = [
            "id", "year", "month", "label", "status",
            "created_by", "created_at", "finished_at", "celery_task_id",
            "total", "count_acm", "count_grid", "count_histo",
            "count_nc", "count_hors_scope",
        ]
        read_only_fields = ["id", "created_at", "finished_at", "celery_task_id",
                            "total", "count_acm", "count_grid", "count_histo",
                            "count_nc", "count_hors_scope"]


class EstimationResultSerializer(serializers.ModelSerializer):
    site_id   = serializers.CharField(source="site.site_id",  read_only=True, default=None)
    site_name = serializers.CharField(source="site.name",     read_only=True, default=None)

    class Meta:
        model  = EstimationResult
        fields = [
            "id", "batch", "site_id", "site_name", "numero_compte_contrat",
            "source_utilisee",
            # FMS
            "acm_disponible", "acm_conso_kwh", "acm_nb_points",
            "grid_disponible", "grid_conso_kwh", "grid_conso_kvah",
            "grid_conso_kvarh", "grid_estimated_kwh", "grid_nb_points",
            "fiabilite_grid", "fiabilite_ratio",
            # Histo
            "histo_disponible", "histo_conso_30j", "histo_nb_mois",
            # Résultat
            "nb_jours_mois", "conso_estimee_kwh", "montant_estime",
            "montant_nrj", "montant_abonnement", "montant_redevance", "montant_tco",
            # Target / Théorique (null pour l'instant)
            "target_conso_kwh", "theorique_conso_kwh",
            # Méta
            "error_message", "computed_at",
        ]


