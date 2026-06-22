from decimal import Decimal

from django.conf import settings
from django.db import models


DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class OptimizationBatch(models.Model):
    """
    Un batch représente un lancement d'optimisation PS & Tarif.
    Il permet de tracer qui a lancé le calcul, quand, et les totaux obtenus.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        DONE = "DONE", "Terminé"
        FAILED = "FAILED", "Échoué"

    launched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimization_batches",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    launched_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    # Paramètres du batch
    only_eligible_sites = models.BooleanField(
        default=True,
        help_text="True = uniquement les sites Aktivco avec grid_fee=True.",
    )

    contracts_count = models.IntegerField(default=0)
    contracts_analyzed = models.IntegerField(default=0)
    contracts_skipped = models.IntegerField(default=0)

    # Résumé optimisation puissance
    optimizable_power_count = models.IntegerField(default=0)
    total_power_gain = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0"),
    )

    # Résumé optimisation tarif
    optimizable_tariff_count = models.IntegerField(default=0)
    total_tariff_gain = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0"),
    )

    # Meilleur gain final
    optimizable_total_count = models.IntegerField(default=0)
    total_best_gain = models.DecimalField(
        max_digits=18,
        decimal_places=3,
        default=Decimal("0"),
    )

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-launched_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["launched_at"]),
        ]

    def __str__(self):
        return f"OptimizationBatch #{self.id} - {self.status}"


class OptimizationResult(models.Model):
    """
    Résultat d'optimisation pour un contrat.
    Une ligne = un numéro de contrat analysé sur son année glissante.
    """

    class TariffFamily(models.TextChoices):
        MT = "MT", "Moyenne tension"
        BT = "BT", "Basse tension"
        UNKNOWN = "UNKNOWN", "Inconnu"

    class BestOptimizationType(models.TextChoices):
        NONE = "NONE", "Aucune optimisation"
        POWER = "POWER", "Optimisation puissance"
        TARIFF = "TARIFF", "Optimisation tarif"
        BOTH = "BOTH", "Puissance + tarif"

    class Status(models.TextChoices):
        OK = "OK", "OK"
        SKIPPED = "SKIPPED", "Ignoré"
        ERROR = "ERROR", "Erreur"

    batch = models.ForeignKey(
        OptimizationBatch,
        on_delete=models.CASCADE,
        related_name="results",
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OK,
        db_index=True,
    )

    # Identité contrat / site
    numero_compte_contrat = models.CharField(max_length=64, db_index=True)

    site = models.ForeignKey(
        "core.Site",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="optimization_results",
    )

    # Snapshot du code site, pour garder l’info même si le lien site change plus tard
    site_code = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)

    # Fenêtre d'analyse
    date_ref = models.DateField(null=True, blank=True)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    invoices_count = models.IntegerField(default=0)
    prorated_invoice_count = models.IntegerField(default=0)

    # Données annuelles calculées
    conso_annuelle = models.DecimalField(**DEC)
    montant_ht_annuel = models.DecimalField(**DEC)

    # Puissance / cosphi
    ps_current = models.DecimalField(**DEC)
    pmax_avg = models.DecimalField(**DEC)
    pmax_max = models.DecimalField(**DEC)
    puissance_transfo = models.DecimalField(**DEC)
    cosphi_avg = models.DecimalField(**DEC)

    # Tarif actuel
    tariff_current = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    tariff_family = models.CharField(
        max_length=16,
        choices=TariffFamily.choices,
        default=TariffFamily.UNKNOWN,
        db_index=True,
    )

    # Facture référence
    facture_reference = models.DecimalField(**DEC)

    # Optimisation puissance
    ps_min_applicable = models.DecimalField(**DEC)
    ps_optimized = models.DecimalField(**DEC)
    facture_power_optimized = models.DecimalField(**DEC)
    gain_power = models.DecimalField(**DEC)

    # Optimisation tarif
    tariff_optimized = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    facture_tariff_optimized = models.DecimalField(**DEC)
    gain_tariff = models.DecimalField(**DEC)

    # Meilleur résultat final
    best_optimization_type = models.CharField(
        max_length=16,
        choices=BestOptimizationType.choices,
        default=BestOptimizationType.NONE,
        db_index=True,
    )

    best_facture_optimized = models.DecimalField(**DEC)
    best_gain = models.DecimalField(**DEC)

    # Détail technique pour audit/debug
    simulation_details = models.JSONField(null=True, blank=True)

    warning_message = models.TextField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-best_gain", "numero_compte_contrat"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "numero_compte_contrat"],
                name="uniq_optimization_result_batch_contract",
            )
        ]
        indexes = [
            models.Index(fields=["batch", "status"]),
            models.Index(fields=["batch", "best_optimization_type"]),
            models.Index(fields=["numero_compte_contrat"]),
            models.Index(fields=["site_id"]),
            models.Index(fields=["tariff_current"]),
            models.Index(fields=["tariff_optimized"]),
            models.Index(fields=["best_gain"]),
        ]

    def __str__(self):
        return (
            f"{self.numero_compte_contrat} | "
            f"{self.site_id or 'N/A'} | "
            f"gain={self.best_gain}"
        )