from django.db import models


class Site(models.Model):
    ZONE_CHOICES = [
        ('DKR', 'Dakar'),
        ('THS', 'Thiès'),
        ('NDM', 'Ndian'),
        ('KDG', 'Kédougou'),
        ('DBL', 'Diourbel'),
        ('KLK', 'Kolda'),
        ('MBR', 'Matam'),
        ('ZIG', 'Ziguinchor'),
        ('BKL', 'Bakel'),
        ('STL', 'Saint-Louis'),
        ('TMB', 'Tambacounda'),
        ('KLD', 'Kaolack'),
        ('ORG', 'Oriental'),
    ]

    COUNTRY_CHOICES = [
        ('sen', 'Sénégal'),
        ('civ', 'Côte d’Ivoire'),
        ('gin', 'Guinée'),
        ('mli', 'Mali'),
        ('bfa', 'Burkina Faso'),
        ('tgo', 'Togo'),
        ('ben', 'Bénin'),
        ('nga', 'Nigeria'),
        ('gha', 'Ghana'),
        ('cmr', 'Cameroun'),
        ('cog', 'Congo'),
        ('gab', 'Gabon'),
        ('cod', 'Congo Démocratique'),
        ('tcd', 'Tchad'),
        ('mar', 'Maroc'),
        ('tun', 'Tunisie'),
        ('dji', 'Djibouti'),
        ('eth', 'Ethiopie'),
        ('ken', 'Kenya'),
        ('uga', 'Ouganda'),
        ('rwa', 'Rwanda'),
        ('zmb', 'Zambie'),
        ('zaf', 'Afrique du Sud'),
        ('moz', 'Mozambique'),
        ('ang', 'Angola'),
        ('nam', 'Namibie'),
        ('zwi', 'Zimbabwe'),
        ('tza', 'Tanzanie'),
    ]

    SCOPE_STATUS = [
        ("IN_SCOPE", "In scope"),
        ("OUT_OF_SCOPE", "Out of scope"),
        ("UNKNOWN", "Unknown"),
    ]

    SITE_KIND = [
        ("INDOOR", "Indoor"),
        ("OUTDOOR", "Outdoor"),
    ]

    # Identité
    site_id = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=255, null=True)

    # Référentiel client / master data
    modernized = models.BooleanField(null=True, blank=True)
    ordered_typology = models.CharField(max_length=255, null=True, blank=True)
    installed_typology = models.CharField(max_length=255, null=True, blank=True)
    billing_typology = models.CharField(max_length=255, null=True, blank=True)

    contract_number = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    meter_number = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    analysis_load = models.IntegerField(null=True, blank=True)
    load_band = models.CharField(max_length=100, null=True, blank=True)

    site_type = models.CharField(max_length=50, choices=SITE_KIND, null=True, blank=True)
    ordered_site_type = models.CharField(max_length=50, choices=SITE_KIND, null=True, blank=True)
    installed_site_type = models.CharField(max_length=50, choices=SITE_KIND, null=True, blank=True)

    configuration = models.CharField(max_length=255, null=True, blank=True)
    target_mapping_key = models.CharField(max_length=255, null=True, blank=True, db_index=True)

    transformer_capacity = models.IntegerField(null=True, blank=True)

    indoor_billed_outdoor = models.BooleanField(null=True, blank=True)
    not_yet_solarized = models.BooleanField(null=True, blank=True)
    solarization_date = models.DateField(null=True, blank=True)

    energy_desk_comment = models.TextField(null=True, blank=True)
    load_comment_category = models.CharField(max_length=100, null=True, blank=True)

    invoice_payment = models.CharField(max_length=100, null=True, blank=True)
    grid_fee = models.BooleanField(null=True, blank=True)
    batch_operational = models.CharField(max_length=100, null=True, blank=True)

    scope_status = models.CharField(
        max_length=20,
        choices=SCOPE_STATUS,
        default="UNKNOWN",
        blank=True,
    )

    meter_status = models.CharField(max_length=50, null=True, blank=True)

    # Métadonnées
    zone = models.CharField(max_length=3, choices=ZONE_CHOICES, null=True, blank=True)
    country = models.CharField(max_length=3, choices=COUNTRY_CHOICES, default='sen')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["site_id"]),
            models.Index(fields=["contract_number"]),
            models.Index(fields=["meter_number"]),
            models.Index(fields=["scope_status"]),
            models.Index(fields=["target_mapping_key"]),
            models.Index(fields=["invoice_payment", "grid_fee"]),
        ]

    def __str__(self):
        return f"{self.site_id} - {self.name}"


class GridTargetRule(models.Model):
    """
    Règles de référence client pour déterminer :
    - la cible énergie grid
    - la cible/jour
    - la redevance attendue
    à partir de la configuration + type + bande de charge
    """

    configuration = models.CharField(max_length=255, db_index=True)
    site_type = models.CharField(max_length=50, null=True, blank=True, db_index=True)   # INDOOR / OUTDOOR
    load_band = models.CharField(max_length=100, null=True, blank=True, db_index=True)

    target_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    target_kwh_per_day = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    grid_fee_amount = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    active = models.BooleanField(default=True)
    source_sheet = models.CharField(max_length=100, null=True, blank=True)
    source_row = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["configuration", "site_type", "load_band"],
                name="uniq_grid_target_rule"
            )
        ]
        indexes = [
            models.Index(fields=["configuration", "site_type", "load_band"]),
        ]

    def __str__(self):
        return f"{self.configuration} | {self.site_type} | {self.load_band}"