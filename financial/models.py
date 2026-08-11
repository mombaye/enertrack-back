# financial/models.py
from django.db import models
from django.conf import settings

DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class FinancialFeeRule(models.Model):
    """
    Catalogue Redevance/Cible Aktivco par (Typologie × Configuration × Load exact).
    Importé depuis le fichier Redevance_et_Cible_Akt.xlsx.

    Clé de lookup : (typology, configuration, load_w)
    Si load exact introuvable → fallback sur le load_w supérieur le plus proche.
    Les lignes avec redevance=0 (typologies HGB, S1..S10, PS) sont ignorées à l'import.
    """

    typology      = models.CharField(max_length=100, db_index=True)
    configuration = models.CharField(max_length=20, db_index=True)   # OUTDOOR / INDOOR
    load_w        = models.IntegerField(db_index=True)                 # valeur exacte en Watts

    redevance     = models.DecimalField(max_digits=18, decimal_places=3)
    cible_kwh     = models.DecimalField(**DEC)
    cible_kwh_j   = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    # traçabilité
    source_filename = models.CharField(max_length=255, null=True, blank=True)
    imported_at     = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "financial"
        constraints = [
            models.UniqueConstraint(
                fields=["typology", "configuration", "load_w"],
                name="uniq_financial_fee_rule",
            )
        ]
        indexes = [
            models.Index(fields=["typology", "configuration", "load_w"]),
        ]

    def __str__(self):
        return f"{self.typology} | {self.configuration} | {self.load_w}W → {self.redevance} FCFA"



class SiteMonthlyLoad(models.Model):
    """
    Historique du Load mensuel réel par site.
    Alimenté via upload CSV (Site_ID | Site_Name | Année | Mois | Load).
    """

    class Source(models.TextChoices):
        ALIGNE       = "aligne",       "Aligné (officiel Sénélec)"
        PREVISIONNEL = "previsionnel", "Prévisionnel (Proposition)"
        MANUAL       = "manual",       "Saisie manuelle"
        IMPORT       = "import",       "Import générique"   # rétrocompat

    site   = models.ForeignKey("core.Site", on_delete=models.CASCADE, related_name="monthly_loads")
    year   = models.IntegerField()
    month  = models.IntegerField()
    load_w = models.IntegerField(help_text="Load réel du site en Watts pour ce mois")
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.IMPORT)

    imported_at = models.DateTimeField(auto_now=True)
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="site_loads_imported"
    )

    class Meta:
        app_label = "financial"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "year", "month"],
                name="uniq_site_monthly_load",
            )
        ]
        indexes = [
            models.Index(fields=["site", "year", "month"]),
            models.Index(fields=["year", "month"]),
        ]

    def __str__(self):
        return f"{self.site.site_id} {self.year}-{self.month:02d} → {self.load_w}W"


class FinancialEvaluation(models.Model):
    """
    Résultat du calcul de marge par site × mois.
    Marge = Redevance Aktivco encaissable – Montant HT facture Sénélec réel.
    """

    class MargeStatut(models.TextChoices):
        OK  = "OK",  "OK"
        NOK = "NOK", "NOK"

    class RecurrenceType(models.TextChoices):
        LIGHT    = "light",    "Light (3-6 mois)"
        CRITIQUE = "critique", "Critique (≥6 mois)"

    class CalculationSource(models.TextChoices):
        PAID_INVOICE       = "PAID_INVOICE", "Factures payées"
        RAW_UNPAID_INVOICE = "RAW_UNPAID_INVOICE", "Factures brutes non payées"
        ESTIMATION_ONLY    = "ESTIMATION_ONLY", "Estimation uniquement"
        NO_DATA            = "NO_DATA", "Aucune donnée"
        
    site  = models.ForeignKey("core.Site", on_delete=models.CASCADE, related_name="financial_evaluations")
    year  = models.IntegerField()
    month = models.IntegerField()

    # ── Données d'entrée snapshotées au moment du calcul ─────────────────────
    load_w        = models.IntegerField(null=True, blank=True)
    typology      = models.CharField(max_length=100, null=True, blank=True)
    configuration = models.CharField(max_length=20, null=True, blank=True)

    # ── Règle redevance utilisée ──────────────────────────────────────────────
    fee_rule       = models.ForeignKey(
        FinancialFeeRule, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="evaluations"
    )
    # load_w effectif de la règle (peut différer si fallback)
    fee_rule_load_w = models.IntegerField(null=True, blank=True)
    redevance       = models.DecimalField(**DEC)
    hors_catalogue  = models.BooleanField(
        default=False,
        help_text="True si la règle vient d'un load supérieur (fallback)"
    )

    # ── Montant facture Sénélec réel ──────────────────────────────────────────
    # Somme de ContractMonth.montant_hors_tva pour ce site × mois
    montant_htva_reel = models.DecimalField(**DEC)


    # ── Nouvelle logique calcul officiel / provisoire ─────────────────────────
    calculation_source = models.CharField(
        max_length=32,
        choices=CalculationSource.choices,
        default=CalculationSource.NO_DATA,
        db_index=True,
        help_text="Source réellement utilisée pour calculer la marge financière."
    )

    is_provisional = models.BooleanField(
        default=True,
        db_index=True,
        help_text="True si le calcul est provisoire : facture non payée ou estimation."
    )

    calculation_warning = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Message d'avertissement à afficher sur l'UI."
    )

    montant_base_calcul = models.DecimalField(
        **DEC,
        help_text="Montant réellement utilisé dans le calcul de la marge."
    )

    montant_factures_payees = models.DecimalField(
        **DEC,
        help_text="Somme HT des factures payées."
    )

    montant_factures_brutes = models.DecimalField(
        **DEC,
        help_text="Somme HT des factures brutes PAID + UNPAID."
    )

    montant_factures_non_payees = models.DecimalField(
        **DEC,
        help_text="Somme HT des factures non payées."
    )

    montant_estime = models.DecimalField(
        **DEC,
        help_text="Montant estimé disponible pour comparaison ou fallback."
    )

    marge_facture_brute = models.DecimalField(
        **DEC,
        help_text="Marge simulée avec les factures brutes."
    )

    marge_estimee = models.DecimalField(
        **DEC,
        help_text="Marge simulée avec l'estimation."
    )

    paid_invoice_count = models.IntegerField(default=0)
    unpaid_invoice_count = models.IntegerField(default=0)
    raw_invoice_count = models.IntegerField(default=0)

    has_invoice_for_month = models.BooleanField(default=False, db_index=True)
    has_unpaid_invoice = models.BooleanField(default=False, db_index=True)

    estimation_source = models.CharField(max_length=32, null=True, blank=True)

    # ── Règle période courte ──────────────────────────────────────────────────
    periode_courte    = models.BooleanField(
        default=False,
        help_text="True si nb_jours_factures < 15 → marge forcée à 0 (site activé/résilié en cours de mois)"
    )
    nb_jours_factures = models.IntegerField(
        null=True, blank=True,
        help_text="Nombre de jours couverts par la facture dans ce mois calendaire"
    )

    # ── Calcul marge ──────────────────────────────────────────────────────────
    marge        = models.DecimalField(**DEC)   # redevance - montant_htva_reel (0 si periode_courte)
    marge_statut = models.CharField(
        max_length=3,
        choices=MargeStatut.choices,
        null=True, blank=True,
        db_index=True,
    )

    # ── Récurrence NOK ────────────────────────────────────────────────────────
    recurrence_mois_nok = models.IntegerField(
        default=0,
        help_text="Nombre de mois consécutifs NOK jusqu'à ce mois inclus"
    )
    recurrence_type = models.CharField(
        max_length=10,
        choices=RecurrenceType.choices,
        null=True, blank=True,
        db_index=True,
        help_text="light = 3-6 mois NOK consécutifs | critique = ≥6 mois"
    )

    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "financial"
        constraints = [
            models.UniqueConstraint(
                fields=["site", "year", "month"],
                name="uniq_financial_evaluation",
            )
        ]
        indexes = [
            models.Index(fields=["site", "year", "month"]),
            models.Index(fields=["year", "month"]),
            models.Index(fields=["marge_statut"]),
            models.Index(fields=["recurrence_type"]),
        ]

    def __str__(self):
        return (
            f"{self.site.site_id} {self.year}-{self.month:02d} "
            f"→ marge={self.marge} [{self.marge_statut}]"
        )


class FinancialConsoMonthly(models.Model):
    """
    Conso FMS/ACM/Solaire mensuelle synchronisée depuis Snowflake (GRID_REPORT,
    AC_METER) et SQL Server (fact_solar_mth) — cf. sync_financial_conso.

    Remplace les appels live à Snowflake/SQL Server faits auparavant à chaque
    requête HTTP (financial/services/conso_service.py) : le site web ne lit
    plus que cette table Postgres, jamais Snowflake en direct.
    """

    class GridMode(models.TextChoices):
        EXACT    = "exact",    "Exact (données denses)"
        EXTRAPOL = "extrapol", "Extrapolé (données éparses)"
        NONE     = "none",     "Aucune donnée"

    site  = models.ForeignKey("core.Site", on_delete=models.CASCADE, related_name="conso_monthly")
    year  = models.IntegerField(db_index=True)
    month = models.IntegerField(db_index=True)

    fms_grid_kwh = models.DecimalField(**DEC)
    grid_mode    = models.CharField(max_length=10, choices=GridMode.choices, default=GridMode.NONE)

    fms_acm_kwh  = models.DecimalField(**DEC)

    solar_kwh      = models.DecimalField(**DEC)
    unavail_hours  = models.DecimalField(**DEC)

    synced_at  = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["site", "year", "month"],
                name="uniq_financial_conso_monthly",
            )
        ]
        indexes = [
            models.Index(fields=["site", "year", "month"]),
            models.Index(fields=["year", "month"]),
        ]

    def __str__(self):
        return f"{self.site.site_id} {self.year}-{self.month:02d} conso"


class FinancialConsoSyncRun(models.Model):
    """Traçabilité d'une exécution de sync_financial_conso (cf. FuelEfmsSyncRun)."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED  = "FAILED",  "Échec"

    month_from = models.CharField(max_length=7, null=True, blank=True)  # YYYY-MM
    month_to   = models.CharField(max_length=7, null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)

    rows_fetched = models.IntegerField(default=0)
    rows_created = models.IntegerField(default=0)
    rows_updated = models.IntegerField(default=0)

    started_at  = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    # Statut par source — _fetch_remote_bulk interroge Snowflake (Grid/ACM) et
    # SQL Server (Solaire) séparément, chacun dans son propre try/except (une
    # panne de l'un n'empêche jamais l'autre d'être synchronisé). error_message
    # ci-dessus ne reflète qu'un échec de la commande dans son ensemble ; ces
    # 2 champs permettent d'afficher le vrai statut de CHAQUE source (ex: le
    # badge "SQL Server déconnecté" sur /financial/suivi-conso).
    snowflake_error = models.TextField(null=True, blank=True)
    solar_error     = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Financial conso sync {self.month_from}→{self.month_to} [{self.status}]"

    
