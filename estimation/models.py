# estimation/models.py

from django.db import models
from django.conf import settings
from django.utils import timezone
from core.models import Site


class EstimationBatch(models.Model):
    """
    Un batch d'estimation = une exécution mensuelle pour tous les sites actifs.
    Créé manuellement ou automatiquement en début de mois.
    """

    class Status(models.TextChoices):
        PENDING  = "PENDING",  "En attente"
        RUNNING  = "RUNNING",  "En cours"
        DONE     = "DONE",     "Terminé"
        FAILED   = "FAILED",   "Échoué"

    # ── Identité du batch ─────────────────────────────────────────────────────
    year  = models.IntegerField("Année")
    month = models.IntegerField("Mois")

    status     = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="estimation_batches",
    )
    created_at  = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)

    # ── Compteurs ─────────────────────────────────────────────────────────────
    total         = models.IntegerField(default=0)
    count_acm     = models.IntegerField(default=0)  # estimés via ACM
    count_grid    = models.IntegerField(default=0)  # estimés via Grid
    count_histo   = models.IntegerField(default=0)  # estimés via historique Sénélec
    count_nc      = models.IntegerField(default=0)  # non calculables
    count_hors_scope = models.IntegerField(default=0)  # sites hors scope (pas de redevance Grid)
    count_senelec = models.IntegerField(default=0)
    count_target = models.IntegerField(default=0)
    count_theorique = models.IntegerField(default=0)


    class Meta:
        unique_together = ("year", "month")
        ordering = ["-year", "-month"]
        verbose_name = "Batch d'estimation"
        verbose_name_plural = "Batchs d'estimation"

    def __str__(self):
        return f"EstimationBatch {self.year}-{self.month:02d} [{self.status}]"

    @property
    def label(self):
        return f"{self.year}-{self.month:02d}"

    def refresh_counters(self):
        from django.db.models import Count

        # Reset avant recalcul pour éviter les anciens compteurs qui restent
        self.count_acm = 0
        self.count_grid = 0
        self.count_senelec = 0
        self.count_target = 0
        self.count_theorique = 0
        self.count_histo = 0
        self.count_nc = 0
        self.count_hors_scope = 0

        agg = self.results.values("source_utilisee").annotate(n=Count("id"))

        mapping = {
            "ACM": "count_acm",
            "GRID": "count_grid",
            "SENELEC": "count_senelec",
            "TARGET": "count_target",
            "THEORIQUE": "count_theorique",
            "HISTO": "count_histo",
            "NC": "count_nc",
            "HORS_SCOPE": "count_hors_scope",
        }

        for row in agg:
            field = mapping.get(row["source_utilisee"])
            if field:
                setattr(self, field, row["n"])

        self.total = self.results.count()


class EstimationResult(models.Model):
    """
    Résultat d'estimation pour un site donné sur un mois donné.
    Une ligne = un site = une provision estimée.
    """

    class Source(models.TextChoices):
        ACM        = "ACM",        "ACM (act_energy_p)"
        GRID       = "GRID",       "Grid Report"
        SENELEC    = "SENELEC",    "Estimation Sénélec"
        HISTO      = "HISTO",      "Historique Sénélec"
        THEORIQUE  = "THEORIQUE",  "Estimation théorique"
        TARGET     = "TARGET",     "Données Target"
        NC         = "NC",         "Non calculable"
        HORS_SCOPE = "HORS_SCOPE", "Hors scope"

    class FiabiliteGrid(models.TextChoices):
        """Résultat des 4 règles de fiabilité CDC Module 2 Étape 3."""
        CORRECT      = "CORRECT",      "Grid Measurement Correct"
        NOT_CORRECT  = "NOT_CORRECT",  "Grid Measurement Not Correct"
        MISSING      = "MISSING",      "Données manquantes"
        NA           = "NA",           "Non applicable (ACM utilisé)"

    # ── Relations ─────────────────────────────────────────────────────────────
    batch = models.ForeignKey(
        EstimationBatch, on_delete=models.CASCADE, related_name="results"
    )
    site = models.ForeignKey(
        Site, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="estimation_results",
    )
    numero_compte_contrat = models.CharField(max_length=64, null=True, blank=True)

    # ── Décision ──────────────────────────────────────────────────────────────
    source_utilisee = models.CharField(
        max_length=16, choices=Source.choices, default=Source.NC,
        verbose_name="Source d'estimation",
    )

    # ── Données FMS brutes (ACM) ──────────────────────────────────────────────
    acm_disponible        = models.BooleanField(default=False)
    acm_conso_kwh         = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    acm_nb_points         = models.IntegerField(null=True, blank=True)

    # ── Données FMS brutes (Grid) ─────────────────────────────────────────────
    grid_disponible       = models.BooleanField(default=False)
    grid_conso_kwh        = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    grid_conso_kvah       = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    grid_conso_kvarh      = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    grid_estimated_kwh    = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                                help_text="Estimated Energy = Pavg * 24 * NbrJrs / 1000 (formule U×I)")
    grid_nb_points        = models.IntegerField(null=True, blank=True)

    # ── Fiabilité Grid (règles CDC étape 3) ───────────────────────────────────
    fiabilite_grid        = models.CharField(
        max_length=16, choices=FiabiliteGrid.choices,
        default=FiabiliteGrid.NA,
    )
    fiabilite_ratio       = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True,
        help_text="Grid kWh / Grid kVAh — facteur de puissance mesuré",
    )

    # ── Données historique Sénélec ────────────────────────────────────────────
    histo_disponible      = models.BooleanField(default=False)
    histo_conso_30j       = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                                help_text="Moyenne des 3 dernières consos facturées normalisée sur 30j")
    histo_nb_mois         = models.IntegerField(null=True, blank=True)

    # ── Résultat final ────────────────────────────────────────────────────────
    nb_jours_mois         = models.IntegerField(null=True, blank=True)
    conso_estimee_kwh     = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                                help_text="Consommation retenue pour le calcul du montant")
    montant_estime        = models.DecimalField(max_digits=16, decimal_places=3, null=True, blank=True,
                                                help_text="Montant HTVA estimé recalculé via billing_check")

    # ── Décomposition du montant (traçabilité) ────────────────────────────────
    montant_nrj           = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    montant_abonnement    = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    montant_redevance     = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    montant_tco           = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    # ── Champs réservés pour Target / Théorique (null pour l'instant) ─────────
    target_conso_kwh      = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    theorique_conso_kwh   = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    # ── Erreur / raison NC ────────────────────────────────────────────────────
    error_message         = models.TextField(null=True, blank=True)

    # ── Méta ──────────────────────────────────────────────────────────────────
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("batch", "site")
        ordering = ["batch", "site__site_id"]
        verbose_name = "Résultat d'estimation"
        verbose_name_plural = "Résultats d'estimation"

    def __str__(self):
        return f"{self.batch.label} | {self.site_id} → {self.source_utilisee} | {self.conso_estimee_kwh} kWh"