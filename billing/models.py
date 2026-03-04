# models.py  ✅ PATCH COMPLET — ajout "données cibles" + table Tarifs (périodée)
from django.db import models
from django.conf import settings

DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class ImportBatch(models.Model):
    class Kind(models.TextChoices):
        SENELEC_INVOICE = "SENELEC_INVOICE", "SENELEC_INVOICE"
        CONTRACT_SITE_LINK = "CONTRACT_SITE_LINK", "CONTRACT_SITE_LINK"
        TARIFF_TABLE = "TARIFF_TABLE", "TARIFF_TABLE"
        TIERS_INVOICE = "TIERS_INVOICE", "TIERS_INVOICE"
        STATUS_UPDATE = "STATUS_UPDATE", "STATUS_UPDATE"   # ✅ Step 7

    kind = models.CharField(
        max_length=32, choices=Kind.choices, default=Kind.SENELEC_INVOICE, db_index=True
    )
    source_filename = models.CharField(max_length=255)
    imported_at = models.DateTimeField(auto_now_add=True)

    # ✅ traçabilité (critère cahier de charge)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="import_batches"
    )

    def __str__(self):
        return f"{self.kind} - {self.source_filename} ({self.imported_at:%Y-%m-%d %H:%M})"



class ImportIssue(models.Model):
    class Severity(models.TextChoices):
        INFO = "INFO", "INFO"
        WARN = "WARN", "WARN"
        ERROR = "ERROR", "ERROR"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="issues")
    row_number = models.IntegerField(null=True, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.WARN)
    field = models.CharField(max_length=64, null=True, blank=True)
    message = models.TextField()
    raw_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["batch", "severity"]),
            models.Index(fields=["batch", "row_number"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"[{self.severity}] batch={self.batch_id} row={self.row_number} {self.field or ''}".strip()


class ContractSiteLink(models.Model):
    """
    Mapping: Numéro contrat -> Site
    """
    site = models.ForeignKey("core.Site", on_delete=models.CASCADE, related_name="contract_links")
    numero_compte_contrat = models.CharField(max_length=32, unique=True, db_index=True)

    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    source_filename = models.CharField(max_length=255, null=True, blank=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"{self.numero_compte_contrat} -> {self.site.site_id}"


class TariffRate(models.Model):
    """
    Table de référence Tarifs (par catégorie + période)
    Ex: DGP / PGP / MTCU / MTG / MTLU
    """
    category = models.CharField(max_length=32, db_index=True)

    energie_k1 = models.DecimalField(**DEC)
    energie_k2 = models.DecimalField(**DEC)
    prime_fixe = models.DecimalField(**DEC)

    date_debut = models.DateField(db_index=True)
    date_fin = models.DateField(db_index=True)

    # ✅ traçabilité
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="tariff_rates"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["category", "date_debut", "date_fin"],
                name="uniq_tariff_rate_period",
            )
        ]
        indexes = [
            models.Index(fields=["category", "date_debut", "date_fin"]),
        ]

    def __str__(self):
        return f"{self.category} ({self.date_debut}→{self.date_fin})"


class SonatelInvoice(models.Model):
    """Ligne brute issue du fichier Sonatel (une ligne = un contrat / une facture / une période)."""

    class Status(models.TextChoices):
        CREATED = "CREATED", "Créée"
        VALIDATED = "VALIDATED", "Validée"
        CONTESTED = "CONTESTED", "Contestée"

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")

    # audit
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # last seen
    last_seen_at = models.DateTimeField(null=True, blank=True)
    last_seen_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="last_seen_rows"
    )

    # Lien Site (Step 5)
    site = models.ForeignKey(
        "core.Site", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sonatel_invoices", db_index=True
    )

    # Identifiants & localisation
    numero_compte_contrat = models.CharField(max_length=32, db_index=True)
    partenaire = models.CharField(max_length=255, null=True, blank=True)
    localite = models.CharField(max_length=255, null=True, blank=True)
    arrondissement = models.CharField(max_length=255, null=True, blank=True)
    rue = models.CharField(max_length=255, null=True, blank=True)

    # Facture
    numero_facture = models.CharField(max_length=64, db_index=True)
    date_comptable_facture = models.DateField(null=True, blank=True)

    # Montants principaux (source)
    montant_total_energie = models.DecimalField(**DEC)
    montant_redevance = models.DecimalField(**DEC)
    montant_tco = models.DecimalField(**DEC)
    montant_hors_tva = models.DecimalField(**DEC)
    montant_tva = models.DecimalField(**DEC)
    montant_ttc = models.DecimalField(**DEC)

    # Période
    date_debut_periode = models.DateField(null=True, blank=True)
    date_fin_periode = models.DateField(null=True, blank=True)

    # Identifiants CG
    ai_cg = models.DecimalField(**DEC)
    ni_cg = models.DecimalField(**DEC)

    # Index/Conso + détails
    ancien_index_k1 = models.DecimalField(**DEC)
    ancien_index_k2 = models.DecimalField(**DEC)
    nouvel_index_k1 = models.DecimalField(**DEC)
    nouvel_index_k2 = models.DecimalField(**DEC)

    montant_energie_k1 = models.DecimalField(**DEC)
    montant_energie_k2 = models.DecimalField(**DEC)

    conso_facturee = models.DecimalField(**DEC)

    rappel_k1 = models.DecimalField(**DEC)
    rappel_k2 = models.DecimalField(**DEC)
    majoration_k1 = models.DecimalField(**DEC)
    majoration_k2 = models.DecimalField(**DEC)

    nb_jour_facturation = models.IntegerField(null=True, blank=True)

    # Puissances / Prime / Cosphi
    puissance_transfo = models.DecimalField(**DEC)
    puissance_souscrite = models.DecimalField(**DEC)      # PS
    puissance_max_relevee = models.DecimalField(**DEC)    # Prel
    montant_prime_fixe = models.DecimalField(**DEC)       # (source)
    montant_cosinus_phi = models.DecimalField(**DEC)      # pénalité cosphi (source)
    valeur_cosinus_phi = models.DecimalField(**DEC)

    # Typologies / classification
    type_de_tarif = models.CharField(max_length=128, null=True, blank=True)  # catégorie tarifaire
    type_de_client = models.CharField(max_length=128, null=True, blank=True)
    ccg = models.CharField(max_length=64, null=True, blank=True)
    type_compte_de_contrat = models.CharField(max_length=128, null=True, blank=True)
    anc_cote = models.CharField(max_length=128, null=True, blank=True)
    unite_de_releve = models.CharField(max_length=64, null=True, blank=True)

    # Réactif
    ancien_index_reactif = models.DecimalField(**DEC)
    nouvel_index_reactif = models.DecimalField(**DEC)
    conso_reactive = models.DecimalField(**DEC)
    majo_reactif = models.DecimalField(**DEC)

    # H1
    ancien_index_h1 = models.DecimalField(**DEC)
    nouvel_index_h1 = models.DecimalField(**DEC)
    conso_h1 = models.DecimalField(**DEC)

    # Divers utiles
    agence = models.CharField(max_length=128, null=True, blank=True)
    numero_compteur = models.CharField(max_length=64, null=True, blank=True)

    # Échéance de paiement
    echeance = models.DateField(null=True, blank=True, db_index=True)

    # ✅ DONNÉES CIBLES (calculées)
    abonnement_calcule = models.DecimalField(**DEC)
    penalite_abonnement_calculee = models.DecimalField(**DEC)
    energie_calculee = models.DecimalField(**DEC)


    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.CREATED,   # ✅ init automatique
        db_index=True,
    )
    status_updated_at = models.DateTimeField(null=True, blank=True)
    status_last_batch = models.ForeignKey(
        ImportBatch, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="status_updates",
    )

   

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["numero_compte_contrat", "numero_facture", "date_debut_periode", "date_fin_periode"],
                name="uniq_sonatel_invoice_row",
            )
        ]
        indexes = [
            models.Index(fields=["numero_compte_contrat", "date_debut_periode", "date_fin_periode"]),
            models.Index(fields=["numero_facture"]),
            models.Index(fields=["site", "date_debut_periode", "date_fin_periode"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.numero_facture} / {self.numero_compte_contrat}"


class MonthlySynthesis(models.Model):
    """Répartition mensuelle (prorata jours) d'une ligne SonatelInvoice."""
    source = models.ForeignKey(SonatelInvoice, on_delete=models.CASCADE, related_name="months")

    year = models.IntegerField()
    month = models.IntegerField()

    period_start = models.DateField()
    period_end = models.DateField()
    period_total_days = models.IntegerField()
    days_covered = models.IntegerField()

    # proratisés (numériques)
    conso = models.DecimalField(**DEC)
    montant_energie = models.DecimalField(**DEC)
    montant_ttc = models.DecimalField(**DEC)
    montant_hors_tva = models.DecimalField(**DEC)

    montant_redevance = models.DecimalField(**DEC)
    montant_tco = models.DecimalField(**DEC)
    montant_tva = models.DecimalField(**DEC)

    montant_energie_k1 = models.DecimalField(**DEC)
    montant_energie_k2 = models.DecimalField(**DEC)
    rappel_k1 = models.DecimalField(**DEC)
    rappel_k2 = models.DecimalField(**DEC)
    majoration_k1 = models.DecimalField(**DEC)
    majoration_k2 = models.DecimalField(**DEC)

    montant_prime_fixe = models.DecimalField(**DEC)
    montant_cosinus_phi = models.DecimalField(**DEC)

    conso_reactive = models.DecimalField(**DEC)
    majo_reactif = models.DecimalField(**DEC)

    conso_h1 = models.DecimalField(**DEC)

    # ✅ DONNÉES CIBLES (proratisées)
    abonnement_calcule = models.DecimalField(**DEC)
    penalite_abonnement_calculee = models.DecimalField(**DEC)
    energie_calculee = models.DecimalField(**DEC)

    # copié (non proratisé)
    valeur_cosinus_phi = models.DecimalField(**DEC)

    # clés fonctionnelles
    numero_compte_contrat = models.CharField(max_length=32, db_index=True)
    numero_facture = models.CharField(max_length=64, db_index=True)

    status = models.CharField(
        max_length=16,
        db_index=True,
        default=SonatelInvoice.Status.CREATED
    )

    class Meta:
        unique_together = ("source", "year", "month")
        indexes = [models.Index(fields=["year", "month", "numero_compte_contrat"]),  models.Index(fields=["year", "month", "status"]),]

    def __str__(self):
        return f"{self.numero_compte_contrat} {self.year}-{self.month:02d}"


class ContractMonth(models.Model):
    """Agrégat par contrat × (année, mois) basé sur MonthlySynthesis."""
    numero_compte_contrat = models.CharField(max_length=32)
    year = models.IntegerField()
    month = models.IntegerField()

    # sommes mensuelles
    conso = models.DecimalField(**DEC)
    montant_energie = models.DecimalField(**DEC)
    montant_ttc = models.DecimalField(**DEC)
    montant_hors_tva = models.DecimalField(**DEC)

    montant_redevance = models.DecimalField(**DEC)
    montant_tco = models.DecimalField(**DEC)
    montant_tva = models.DecimalField(**DEC)

    montant_energie_k1 = models.DecimalField(**DEC)
    montant_energie_k2 = models.DecimalField(**DEC)
    rappel_k1 = models.DecimalField(**DEC)
    rappel_k2 = models.DecimalField(**DEC)
    majoration_k1 = models.DecimalField(**DEC)
    majoration_k2 = models.DecimalField(**DEC)

    montant_prime_fixe = models.DecimalField(**DEC)
    montant_cosinus_phi = models.DecimalField(**DEC)

    conso_reactive = models.DecimalField(**DEC)
    majo_reactif = models.DecimalField(**DEC)
    conso_h1 = models.DecimalField(**DEC)

    # ✅ DONNÉES CIBLES (sommées)
    abonnement_calcule = models.DecimalField(**DEC)
    penalite_abonnement_calculee = models.DecimalField(**DEC)
    energie_calculee = models.DecimalField(**DEC)

    # optionnel : moyenne
    valeur_cosinus_phi = models.DecimalField(**DEC)

    invoices_count = models.IntegerField(default=0)
    first_period_start = models.DateField(null=True, blank=True)
    last_period_end = models.DateField(null=True, blank=True)

    
    validated_count = models.IntegerField(default=0)
    contested_count = models.IntegerField(default=0)
    

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["numero_compte_contrat", "year", "month"], name="uniq_contract_month")
        ]
        indexes = [
            models.Index(fields=["numero_compte_contrat", "year", "month"]),
            models.Index(fields=["year", "month"]),
        ]

    def __str__(self):
        return f"{self.numero_compte_contrat} {self.year}-{self.month:02d}"


class ConsumptionLine(models.Model):
    class Granularity(models.TextChoices):
        DAY = "DAY", "Day"
        MONTH = "MONTH", "Month"

    batch = models.ForeignKey("billing.ImportBatch", on_delete=models.CASCADE, related_name="consumption_lines")
    numero_compte_contrat = models.CharField(max_length=32, db_index=True)

    granularity = models.CharField(max_length=8, choices=Granularity.choices, db_index=True)

    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)

    k1 = models.DecimalField(**DEC)
    k2 = models.DecimalField(**DEC)

    source_filename = models.CharField(max_length=255, null=True, blank=True)
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "numero_compte_contrat", "granularity", "period_start"],
                name="uniq_cons_batch_contract_gran_start",
            )
        ]


class PreInvoice(models.Model):
    class Granularity(models.TextChoices):
        DAY = "DAY", "Day"
        MONTH = "MONTH", "Month"

    batch = models.ForeignKey("billing.ImportBatch", on_delete=models.CASCADE, related_name="preinvoices")
    contract_link = models.ForeignKey("billing.ContractSiteLink", on_delete=models.PROTECT, related_name="preinvoices")

    numero_compte_contrat = models.CharField(max_length=32, db_index=True)

    granularity = models.CharField(max_length=8, choices=Granularity.choices, db_index=True)
    period_start = models.DateField(db_index=True)
    period_end = models.DateField(db_index=True)

    category = models.CharField(max_length=32, db_index=True)
    tariff = models.ForeignKey("billing.TariffRate", null=True, blank=True, on_delete=models.SET_NULL)

    k1 = models.DecimalField(**DEC)
    k2 = models.DecimalField(**DEC)

    unit_k1 = models.DecimalField(**DEC)
    unit_k2 = models.DecimalField(**DEC)
    prime_fixe = models.DecimalField(**DEC)

    amount_k1 = models.DecimalField(**DEC)
    amount_k2 = models.DecimalField(**DEC)
    total = models.DecimalField(**DEC)

    status = models.CharField(max_length=16, default="DRAFT", db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "numero_compte_contrat", "granularity", "period_start"],
                name="uniq_preinv_batch_contract_gran_start",
            )
        ]