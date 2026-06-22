# certification/serializers.py
# PATCH v4.1 — ajout flag_mesure_alert + mesure_alert + mesure_alert_pct

from rest_framework import serializers
from .models import CertificationBatch, CertificationResult, EfmsConnectionLog


class CertificationBatchSerializer(serializers.ModelSerializer):
    echeance = serializers.CharField(source="echeance_label", read_only=True)
    launched_by_username = serializers.CharField(
        source="launched_by.username", read_only=True, default=None
    )

    class Meta:
        model  = CertificationBatch
        fields = [
            "id", "import_batch", "echeance", "echeance_year", "echeance_month",
            "status", "celery_task_id",
            "launched_by", "launched_by_username", "launched_at", "finished_at",
            "total", "certified_fms", "certified_senelec",
            "needs_review", "unknown_contract", "fms_unavailable",
            "mesure_alert",  # ✅ v4 — compteur alerte mesure
        ]
        read_only_fields = fields


class CertificationBatchDetailSerializer(CertificationBatchSerializer):
    breakdown = serializers.SerializerMethodField()

    class Meta(CertificationBatchSerializer.Meta):
        fields = CertificationBatchSerializer.Meta.fields + ["breakdown"]

    def get_breakdown(self, obj):
        total = obj.total or 1
        return {
            "certified_fms_pct":     round(obj.certified_fms     / total * 100, 1),
            "certified_senelec_pct": round(obj.certified_senelec / total * 100, 1),
            "needs_review_pct":      round(obj.needs_review      / total * 100, 1),
            "unknown_contract_pct":  round(obj.unknown_contract  / total * 100, 1),
            "fms_unavailable_pct":   round(obj.fms_unavailable   / total * 100, 1),
            "mesure_alert_pct":      round((obj.mesure_alert or 0) / total * 100, 1),  # ✅ v4
        }


class CertificationResultSerializer(serializers.ModelSerializer):
    # Infos site
    site_id   = serializers.CharField(source="site.site_id", read_only=True, default=None)
    site_name = serializers.CharField(source="site.name",    read_only=True, default=None)

    # Infos facture
    numero_facture        = serializers.CharField(source="invoice.numero_facture",        read_only=True)
    numero_compte_contrat = serializers.CharField(source="invoice.numero_compte_contrat", read_only=True)
    date_debut_periode    = serializers.DateField(source="invoice.date_debut_periode",    read_only=True)
    date_fin_periode      = serializers.DateField(source="invoice.date_fin_periode",      read_only=True)
    montant_ttc           = serializers.DecimalField(
        source="invoice.montant_ttc", max_digits=18, decimal_places=3,
        read_only=True, default=None,
    )
    montant_hors_tva      = serializers.DecimalField(
        source="invoice.montant_hors_tva", max_digits=18, decimal_places=3,
        read_only=True, default=None,
    )

    class Meta:
        model  = CertificationResult
        fields = [
            "id", "cert_batch", "invoice",
            "numero_facture", "numero_compte_contrat",
            "date_debut_periode", "date_fin_periode",
            "montant_ttc", "montant_hors_tva",
            "site", "site_id", "site_name",

            # Statut
            "status", "certified_by_rule", "computed_at",

            # Étape 4
            "conso_facturee_periode", "nb_jours_facturation", "conso_facturee_30j",

            # Étape 5 — FMS Grid (source PRIMAIRE)
            "fms_available", "conso_fms_periode", "conso_fms_30j",
            "fms_last_complete_month", "fms_error",

            # Étape 6 — Historique
            "histo_last_conso", "histo_3mois_avg",

            # Étape 7A — Ratios FMS Grid
            "ratio_fms_periode", "ratio_fms_30j", "ratio_histo_3mois",

            # Étape 7B — ACM (fallback si Grid absent)
            "acm_available",
            "estim_conso_acm_periode", "estim_conso_acm_30j",
            "ratio_acm_periode", "ratio_acm_30j",
            "acm_error",

            # ✅ Étape 9 — Alerte mesure v4.1
            # TRUE si Conso_Facturée < 50% OU > 150% de Conso_FMS ou Conso_Histo
            "flag_mesure_alert",

            # Étape 10 — Cohérence montant
            "montant_htva_calcule",
            "variation_montant_pct",
            "montant_coherent",
            "montant_check_error",
        ]
        read_only_fields = fields


class EfmsConnectionLogSerializer(serializers.ModelSerializer):
    class Meta:
        model  = EfmsConnectionLog
        fields = [
            "id", "cert_batch", "attempted_at",
            "status", "duration_ms", "host", "query", "error",
        ]
        read_only_fields = fields