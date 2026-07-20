from django.conf import settings
from django.db import models

DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class CategorieBO(models.TextChoices):
    SITE_OFF_GRID          = "site_off_grid", "Site off-grid"
    FONCTIONNEMENT_OPTIMAL = "fonctionnement_optimal", "Fonctionnement optimal"
    DYSFONCTIONNEMENT      = "dysfonctionnement", "Dysfonctionnement & correction materials"
    BUREAUX_SONATEL        = "bureaux_sonatel", "Site avec bureaux, facturation par Sonatel"
    INSTALLATION_MATERIELS = "installation_materiels", "Installation des matériels"
    OPTIMAL_OBSERVATION    = "optimal_observation", "Fonctionnement optimal sous observation (faible marge négative)"
    HORS_SCOPE_ESCO        = "hors_scope_esco", "Site hors scope ESCO"
    VERIF_FACTURATION      = "verif_facturation_senelec", "Vérification facturation SENELEC"
    PASS_THROUGH           = "pass_through", "Site_OG en pass-through"
    CHANGEMENT_TYPOLOGIE   = "changement_typologie", "Changement de typologie à faire"
    CHANGEMENT_TARIF       = "changement_tarif", "Changement Tarif avec SENELEC"
    TYPOLOGIE_VALIDEE      = "typologie_validee_attente", "Changement typologie validé, attente matériels PV + PWC"
    AUTRE                  = "autre", "Autre"


class ActionOwner(models.TextChoices):
    RAS          = "ras", "RAS"
    OM           = "om", "O&M"
    ROT          = "rot", "ROT"
    GRID_MANAGER = "grid_manager", "Grid Manager"
    ADEL         = "adel", "ADEL"
    AUTRE        = "autre", "Autre"


class BOMarginSnapshot(models.Model):
    """
    Import figé (lecture seule) du fichier Analyse Marge.xlsx.
    Instantané historique de référence — pas de pipeline d'import récurrent.
    """

    site = models.ForeignKey(
        "core.Site", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bo_margin_snapshots",
    )
    site_id_raw   = models.CharField(max_length=50, db_index=True)
    site_name_raw = models.CharField(max_length=255, blank=True)
    zone          = models.CharField(max_length=50, blank=True)
    typologie_reelle = models.CharField(max_length=100, blank=True)

    # Colonnes comparatives mois A / mois B (format large du fichier source)
    month_a_label = models.CharField(max_length=20, blank=True)
    month_b_label = models.CharField(max_length=20, blank=True)

    puissance_facturee_a   = models.DecimalField(**DEC)
    puissance_facturee_b   = models.DecimalField(**DEC)
    redevance_grid_a       = models.DecimalField(**DEC)
    redevance_grid_b       = models.DecimalField(**DEC)
    estimation_conso_kwh_a = models.DecimalField(**DEC)
    estimation_conso_kwh_b = models.DecimalField(**DEC)
    estimation_conso_xof_a = models.DecimalField(**DEC)
    estimation_conso_xof_b = models.DecimalField(**DEC)
    redevance_vs_estimation_a = models.DecimalField(**DEC)
    redevance_vs_estimation_b = models.DecimalField(**DEC)
    statut_marge = models.CharField(max_length=20, blank=True)

    grid_action_plan_ops = models.TextField(blank=True)
    categorie_ops        = models.CharField(max_length=255, blank=True)

    # Colonnes remplies par le BO dans le fichier source
    commentaire_bo     = models.TextField(blank=True)
    categorie_bo       = models.CharField(max_length=40, choices=CategorieBO.choices, blank=True)
    categorie_bo_autre = models.CharField(max_length=255, blank=True)
    check_done         = models.BooleanField(default=False)
    action_owner       = models.CharField(max_length=20, choices=ActionOwner.choices, blank=True)
    action_owner_autre = models.CharField(max_length=255, blank=True)
    commentaire        = models.TextField(blank=True)

    source_filename = models.CharField(max_length=255, blank=True)
    imported_at     = models.DateTimeField(auto_now_add=True)
    row_number      = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["site_id_raw"]),
            models.Index(fields=["categorie_bo"]),
            models.Index(fields=["zone"]),
        ]

    def __str__(self):
        return f"{self.site_id_raw} — {self.categorie_bo or '—'}"


class BOAnalysisRequest(models.Model):
    """
    Demande d'analyse BO activée par un user (admin/analyst) sur un site pour une période donnée.
    Pas de contrainte d'unicité (site, year, month) : historique cumulatif, ré-activation possible.
    """

    class Status(models.TextChoices):
        PENDING     = "pending", "En attente"
        IN_PROGRESS = "in_progress", "En cours"
        DONE        = "done", "Terminée"

    site  = models.ForeignKey("core.Site", on_delete=models.CASCADE, related_name="bo_requests")
    year  = models.IntegerField()
    month = models.IntegerField()

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bo_requests_made"
    )
    assigned_bo = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bo_requests_assigned",
        help_text="BO qui a effectivement réclamé/soumis — posé automatiquement à la soumission, jamais à la création.",
    )
    targeted_bos = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name="bo_requests_targeted",
        help_text="BO ciblés à la création. Vide = diffusion à tous les BO actifs.",
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    notes  = models.TextField(blank=True)

    requested_at = models.DateTimeField(auto_now_add=True)
    due_at       = models.DateTimeField(null=True, blank=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["site", "year", "month"]),
            models.Index(fields=["assigned_bo", "status"]),
            models.Index(fields=["status"]),
        ]
        ordering = ["-requested_at"]

    def __str__(self):
        return f"{self.site.site_id} {self.year}-{self.month:02d} [{self.status}]"


class BOAnalysis(models.Model):
    """L'analyse effectivement remplie et enregistrée par le BO pour une demande donnée."""

    request = models.OneToOneField(
        BOAnalysisRequest, on_delete=models.CASCADE, related_name="analysis"
    )

    categorie_bo       = models.CharField(max_length=40, choices=CategorieBO.choices)
    categorie_bo_autre = models.CharField(max_length=255, blank=True)
    commentaire_bo     = models.TextField(blank=True)
    action_owner       = models.CharField(max_length=20, choices=ActionOwner.choices)
    action_owner_autre = models.CharField(max_length=255, blank=True)
    commentaire        = models.TextField(blank=True)
    check_done         = models.BooleanField(default=False)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bo_analyses_submitted"
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Analyse BO #{self.request_id} — {self.categorie_bo}"
