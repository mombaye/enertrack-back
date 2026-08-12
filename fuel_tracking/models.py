from django.db import models
from django.utils import timezone


DECIMAL_KWARGS = dict(max_digits=18, decimal_places=3, default=0)


class FuelEfmsMonthly(models.Model):
    """
    Donnée mensuelle consolidée eFMS Fuel par site.

    Source SQL :
    - silver.fact_fuel_order_mth
    - silver.fact_fuel_deli_mth
    - silver.fact_fuel_conso_mth
    - silver.fact_genset_mth
    """

    month_year = models.CharField(max_length=7, db_index=True)  # YYYY-MM
    year = models.IntegerField(db_index=True)
    month = models.IntegerField(db_index=True)

    country = models.CharField(max_length=64, db_index=True)
    site_id = models.CharField(max_length=128, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)

    fuel_order_l = models.DecimalField(**DECIMAL_KWARGS)
    fuel_deli_l = models.DecimalField(**DECIMAL_KWARGS)
    fuel_conso_l = models.DecimalField(**DECIMAL_KWARGS)

    ge_working_hours = models.DecimalField(**DECIMAL_KWARGS)
    abnormal_ge_working_hours = models.DecimalField(**DECIMAL_KWARGS)
    monitoring_unavailability_hours = models.DecimalField(**DECIMAL_KWARGS)
    monitoring_unavailability_percent = models.DecimalField(**DECIMAL_KWARGS)

    rh_hours = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True,
        help_text="RH calculé via la cascade Snowflake (DSE/redresseur/GE_STATUS) ou ENOC en secours.",
    )
    rh_source = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    avec_dse = models.BooleanField(null=True, blank=True)

    cph_l_per_hour = models.DecimalField(
        max_digits=18,
        decimal_places=6,
        null=True,
        blank=True,
        help_text="CPH réel = fuel_conso_l / ge_working_hours",
    )

    stock_ouv_rms_l = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True,
        help_text="Niveau de cuve RMS (IM_GENERATOR_FUEL_LEVEL) le plus proche du 1er du mois.",
    )
    stock_ouv_rms_at = models.DateTimeField(null=True, blank=True)
    stock_clot_rms_l = models.DecimalField(
        max_digits=18, decimal_places=3, null=True, blank=True,
        help_text="Niveau de cuve RMS (IM_GENERATOR_FUEL_LEVEL) le plus proche du dernier jour du mois.",
    )
    stock_clot_rms_at = models.DateTimeField(null=True, blank=True)

    anomaly_flags = models.JSONField(default=list, blank=True)

    synced_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "eFMS Fuel mensuel"
        verbose_name_plural = "eFMS Fuel mensuel"
        ordering = ["-year", "-month", "site_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["country", "month_year", "site_id"],
                name="uniq_fuel_efms_monthly_country_month_site",
            )
        ]
        indexes = [
            models.Index(fields=["country", "year", "month"]),
            models.Index(fields=["country", "site_id"]),
            models.Index(fields=["month_year", "site_id"]),
        ]

    def __str__(self):
        return f"{self.country} | {self.month_year} | {self.site_id}"


class FuelEfmsSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    country = models.CharField(max_length=64, default="Senegal")
    month_from = models.CharField(max_length=7, null=True, blank=True)
    month_to = models.CharField(max_length=7, null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )


    rows_fetched = models.IntegerField(default=0)
    rows_created = models.IntegerField(default=0)
    rows_updated = models.IntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Fuel eFMS sync {self.country} {self.month_from}→{self.month_to} [{self.status}]"




class FuelEnocMovement(models.Model):
    """
    Mouvement réel de ravitaillement provenant de ENOC.

    Source :
    GET /fuel/integrations/enertrack/operations
    """

    source_system = models.CharField(max_length=32, default="ENOC", db_index=True)
    source_id = models.CharField(max_length=128, db_index=True)

    request_id = models.CharField(max_length=128, null=True, blank=True)
    request_code = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    status = models.CharField(max_length=32, default="done", db_index=True)

    site_id = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    zone = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    ville = models.CharField(max_length=128, null=True, blank=True)

    operation_type = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    operation_date = models.DateTimeField(null=True, blank=True, db_index=True)

    requested_quantity_liters = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    approved_quantity_liters = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    quantity_added_liters = models.DecimalField(max_digits=18, decimal_places=3, default=0)

    level_before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    level_before_unit = models.CharField(max_length=16, null=True, blank=True)

    level_after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    level_after_unit = models.CharField(max_length=16, null=True, blank=True)

    hour_meter_before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    hour_meter_after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    monthly_target_liters = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    monthly_total_after_liters = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    target_percent_after = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    target_status = models.CharField(max_length=32, null=True, blank=True)
    is_target_exceeded = models.BooleanField(default=False)

    ge_snapshot = models.JSONField(default=dict, blank=True)
    ponction = models.JSONField(null=True, blank=True)

    technician_name = models.CharField(max_length=255, null=True, blank=True)
    technician_phone = models.CharField(max_length=64, null=True, blank=True)
    team = models.CharField(max_length=128, null=True, blank=True)
    teammate = models.CharField(max_length=255, null=True, blank=True)
    rm = models.CharField(max_length=255, null=True, blank=True)

    created_by = models.CharField(max_length=255, null=True, blank=True)
    validated_by = models.CharField(max_length=255, null=True, blank=True)
    done_by = models.CharField(max_length=255, null=True, blank=True)

    created_at_source = models.DateTimeField(null=True, blank=True)
    validated_at_source = models.DateTimeField(null=True, blank=True)
    done_at_source = models.DateTimeField(null=True, blank=True)
    source_created_at = models.DateTimeField(null=True, blank=True)
    source_updated_at = models.DateTimeField(null=True, blank=True)

    import_source = models.CharField(max_length=128, null=True, blank=True)
    import_key = models.CharField(max_length=255, null=True, blank=True)

    delivery_note_number = models.CharField(max_length=128, null=True, blank=True)
    delivery_note_quantity_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    supplier = models.CharField(max_length=255, null=True, blank=True)
    gauging_method = models.CharField(max_length=128, null=True, blank=True)
    rms_level_before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    rms_level_after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    comment = models.TextField(null=True, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    synced_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-operation_date", "site_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["source_system", "source_id"],
                name="uniq_fuel_enoc_movement_source",
            )
        ]
        indexes = [
            models.Index(fields=["source_system", "source_id"]),
            models.Index(fields=["site_id", "operation_date"]),
            models.Index(fields=["zone", "operation_date"]),
            models.Index(fields=["operation_type", "operation_date"]),
        ]

    def __str__(self):
        return f"{self.source_system} | {self.request_code or self.source_id} | {self.site_id}"


class FuelEnocSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    updated_since = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.RUNNING,
        db_index=True,
    )

    rows_fetched = models.IntegerField(default=0)
    rows_created = models.IntegerField(default=0)
    rows_updated = models.IntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"ENOC Fuel sync {self.start_date}→{self.end_date} [{self.status}]"


class FuelSiteScope(models.Model):
    """
    Périmètre des sites réellement concernés par le suivi fuel (sites avec GE
    installé — Off-Grid ou Hybride). Le parc complet compte ~3300 sites mais
    seuls ceux avec un GE consomment du fuel ; les sites On-Grid sans genset
    (PS/GG-SO/GG-NG) n'ont rien à suivre ici.

    Volontairement séparé de core.Site (qui sert financial/certification/billing
    et n'a pas ce concept) pour ne pas complexifier ce modèle avec une notion
    propre au module fuel.
    """

    class Source(models.TextChoices):
        CURATED_OPS_LIST = "CURATED_OPS_LIST", "Liste opérationnelle validée"
        TYPOLOGY_CROSSWALK = "TYPOLOGY_CROSSWALK", "Règle typologie (catalogue installé)"

    site_id = models.CharField(max_length=128, unique=True, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)

    has_genset = models.BooleanField(db_index=True)
    catalogue_typology = models.CharField(max_length=32, null=True, blank=True)
    billing_typology = models.CharField(max_length=64, null=True, blank=True)

    source = models.CharField(max_length=32, choices=Source.choices)

    imported_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Périmètre fuel (site avec GE)"
        verbose_name_plural = "Périmètre fuel (sites avec GE)"
        ordering = ["site_id"]

    def __str__(self):
        return f"{self.site_id} | GE={'oui' if self.has_genset else 'non'} [{self.source}]"


class GensetFuelCurve(models.Model):
    """
    Catalogue de consommation fuel par modèle de GE (feuille "GENSET DB" du
    fichier de synthèse Ops). Pour chaque modèle, la conso à 100/75/50% de
    charge est mesurée, puis une régression quadratique conso(x) = a·x² + b·x + c
    (x = % de charge) est ajustée sur ces 3 points — c'est cette courbe qui
    donne la conso théorique réelle, bien plus précise qu'un simple ratio
    linéaire charge/puissance.

    ENOC ne remonte que la marque + puissance (kVA) du GE installé, jamais le
    modèle précis (ex: pas moyen de distinguer un FG Wilson P50-3 d'un P50-4
    à 45 kVA) — le matching se fait donc par (marque, kVA), voir
    services/genset_curve_matching.py. Quand plusieurs modèles partagent la
    même (marque, kVA) avec des courbes différentes, toutes les variantes sont
    gardées ici et le matching moyenne/flag l'ambiguïté au moment du calcul.
    """

    manufacturer = models.CharField(max_length=64)
    manufacturer_normalized = models.CharField(max_length=64, db_index=True)
    type_de_ge = models.CharField(max_length=64)
    genset_list = models.CharField(max_length=255, null=True, blank=True)

    voltage_v = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    phases = models.IntegerField(null=True, blank=True)
    prp_kva = models.DecimalField(max_digits=10, decimal_places=2, db_index=True)
    prp_kw = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cosphi = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    conso_100_l_h = models.DecimalField(max_digits=10, decimal_places=3)
    conso_75_l_h = models.DecimalField(max_digits=10, decimal_places=3)
    conso_50_l_h = models.DecimalField(max_digits=10, decimal_places=3)

    coef_a = models.DecimalField(max_digits=14, decimal_places=6)
    coef_b = models.DecimalField(max_digits=14, decimal_places=6)
    coef_c = models.DecimalField(max_digits=14, decimal_places=6)

    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Courbe conso GE (catalogue)"
        verbose_name_plural = "Courbes conso GE (catalogue)"
        ordering = ["manufacturer_normalized", "prp_kva"]
        indexes = [
            models.Index(fields=["manufacturer_normalized", "prp_kva"]),
        ]

    def __str__(self):
        return f"{self.manufacturer} {self.type_de_ge} ({self.prp_kva} kVA)"

    def conso_l_h_at(self, charge_pct: float) -> float:
        """conso(x) = a·x² + b·x + c, x en fraction (1.0 = 100% de charge)."""
        x = float(charge_pct)
        return float(self.coef_a) * x * x + float(self.coef_b) * x + float(self.coef_c)


class CphMatrixPoint(models.Model):
    """
    Point de la matrice CPH (feuille "CPH" du fichier Suivi Ravitaillement) :
    conso horaire mesurée (L/h) par (famille moteur, puissance nominale kVA,
    % de charge). Contrairement à GensetFuelCurve (courbe quadratique ajustée
    sur 3 points, par marque+modèle précis), cette matrice donne 20 points de
    mesure réels par taille de moteur — plus fine sur l'axe %charge, mais
    seule la famille "Perkins" est actuellement renseignée dans le fichier
    source (Kohler/Mitsubishi/... sont des blocs vides, pas importés).
    """

    engine_family = models.CharField(max_length=64, db_index=True)
    engine_family_normalized = models.CharField(max_length=64, db_index=True)
    dg_capacity_kva = models.DecimalField(max_digits=10, decimal_places=2, db_index=True)
    charge_pct = models.DecimalField(max_digits=6, decimal_places=4)
    cph_l_h = models.DecimalField(max_digits=10, decimal_places=4)

    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Point matrice CPH"
        verbose_name_plural = "Points matrice CPH"
        ordering = ["engine_family_normalized", "dg_capacity_kva", "charge_pct"]
        constraints = [
            models.UniqueConstraint(
                fields=["engine_family_normalized", "dg_capacity_kva", "charge_pct"],
                name="uniq_cph_matrix_point",
            )
        ]
        indexes = [
            models.Index(fields=["engine_family_normalized", "dg_capacity_kva"]),
        ]

    def __str__(self):
        return f"{self.engine_family} {self.dg_capacity_kva} kVA @ {float(self.charge_pct):.0%} = {self.cph_l_h} L/h"


class FuelCommandeSynthese(models.Model):
    """
    Snapshot mensuel de la feuille "Synthèse Commande" du fichier Excel
    "Commande FUEL ESCO SENEGAL <mois>.xlsb" — import brut, sans recalcul :
    chaque ligne reprend telle quelle une ligne du tableau (par catégorie/
    batch ou par typologie facturée), avec les colonnes du mois courant,
    du mois précédent, et l'écart, déjà calculées dans le fichier source.
    """

    class GroupType(models.TextChoices):
        CATEGORIE = "CATEGORIE", "Par catégorie / batch"
        TYPOLOGIE = "TYPOLOGIE", "Par typologie facturée"

    month_year = models.CharField(max_length=7, db_index=True)  # mois courant, YYYY-MM
    prev_month_year = models.CharField(max_length=7, null=True, blank=True)

    group_type = models.CharField(max_length=16, choices=GroupType.choices, db_index=True)
    order_index = models.IntegerField()  # ordre d'apparition dans la feuille source
    label = models.CharField(max_length=128)
    is_total_row = models.BooleanField(default=False)  # TOTAL SITES / TOTAL COMMANDE / etc.

    # Mois courant
    nb_sites = models.DecimalField(**DECIMAL_KWARGS)
    commande_normale_l = models.DecimalField(**DECIMAL_KWARGS)
    commande_hivernale_l = models.DecimalField(**DECIMAL_KWARGS)
    total_l = models.DecimalField(**DECIMAL_KWARGS)

    # Mois précédent
    nb_sites_prev = models.DecimalField(**DECIMAL_KWARGS)
    commande_normale_prev_l = models.DecimalField(**DECIMAL_KWARGS)
    commande_hivernale_prev_l = models.DecimalField(**DECIMAL_KWARGS)
    total_prev_l = models.DecimalField(**DECIMAL_KWARGS)

    # Écart (déjà calculé dans le fichier source)
    ecart_sites = models.DecimalField(**DECIMAL_KWARGS)
    ecart_qte_l = models.DecimalField(**DECIMAL_KWARGS)

    commentaires = models.TextField(null=True, blank=True)

    source_filename = models.CharField(max_length=255, null=True, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Synthèse commande carburant (import mensuel)"
        verbose_name_plural = "Synthèses commande carburant (imports mensuels)"
        ordering = ["-month_year", "group_type", "order_index"]
        indexes = [
            models.Index(fields=["month_year", "group_type"]),
        ]

    def __str__(self):
        return f"{self.month_year} · {self.group_type} · {self.label}"


class FuelSuiviCommandeSite(models.Model):
    """
    Snapshot mensuel par site de la feuille "Suivis commande" du fichier
    Excel "Commande FUEL ESCO SENEGAL <mois>" — import brut, sans recalcul,
    limité aux colonnes mises en évidence en bleu (fond bleu, thème "Accent 1")
    dans le fichier source : ce sont les variables que l'équipe Ops considère
    importantes sur cette feuille très large (139 colonnes au total). Même
    fichier que FuelCommandeSynthese, même mois — importé en même temps.
    """

    month_year = models.CharField(max_length=7, db_index=True)  # YYYY-MM

    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    typologie_contractuelle = models.CharField(max_length=128, null=True, blank=True)
    load_commande = models.DecimalField(**DECIMAL_KWARGS)
    indoor_outdoor = models.CharField(max_length=32, null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    batch = models.CharField(max_length=128, null=True, blank=True)
    typologie_facturee = models.CharField(max_length=128, null=True, blank=True)
    conso_moy_jour_l = models.DecimalField(**DECIMAL_KWARGS)
    commande_sans_marge_l = models.DecimalField(**DECIMAL_KWARGS)
    commande_avec_marge_l = models.DecimalField(**DECIMAL_KWARGS)
    estimation_stock_final_l = models.DecimalField(**DECIMAL_KWARGS)
    typo_operations = models.CharField(max_length=128, null=True, blank=True)

    source_filename = models.CharField(max_length=255, null=True, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Suivi commande carburant par site (import mensuel)"
        verbose_name_plural = "Suivis commande carburant par site (imports mensuels)"
        ordering = ["-month_year", "site_id"]
        indexes = [
            models.Index(fields=["month_year", "site_id"]),
        ]

    def __str__(self):
        return f"{self.month_year} · {self.site_id}"


class FuelConsommationMonthly(models.Model):
    """
    Consommation carburant mensuelle par site — automatisée, jointure de
    plusieurs sources (voir fuel_tracking/services/fuel_consommation_snowflake.py
    et la commande sync_fuel_consommation) :
      - Snowflake DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT (conso MESURÉE,
        depuis le 2026-08) + DB_GFMS_PROD.GOLD.GE_PROD_KWH (conso spécifique,
        ratio pondéré) ; agrégées sur le mois, jointes à SITE_FILTERED pour
        résoudre site_id ;
      - ENOC (FuelEnocMovement, demandes de ravitaillement validées par le
        fuel manager, déjà synchronisées via sync_enoc_fuel_movements ; plus
        fetch_estimated_consumption pour conso_estimee_enoc_l, filtré depuis
        le 2026-08 sur les ravitaillements liés à une demande validée) ;
      - fichiers mensuels remontés par les gardiens (pas encore intégré —
        colonnes prévues mais laissées vides tant que le format n'est pas défini).

    Contrairement à FuelCommandeSynthese/FuelSuiviCommandeSite (import manuel,
    verbatim), ce modèle est calculé : re-synchroniser un mois remplace
    entièrement ses lignes (mêmes garanties que FinancialConsoMonthly).
    """

    month_year = models.CharField(max_length=7, db_index=True)  # YYYY-MM
    year = models.IntegerField(db_index=True)
    month = models.IntegerField(db_index=True)

    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    country = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # Snowflake — DB_GFMS_PROD.GOLD.SITE_FILTERED (dimension site)
    typology = models.CharField(max_length=64, null=True, blank=True)
    site_type = models.CharField(max_length=64, null=True, blank=True)
    dg_count = models.CharField(max_length=16, null=True, blank=True, help_text="Nombre de groupes électrogènes installés sur le site.")
    power_supply = models.CharField(max_length=64, null=True, blank=True, help_text="Ex: Grid+DG, DG+Solar, Grid+DG+Solar.")

    # Présence GE — jointure Snowflake (dg_count) + ENOC (sites.nb_ge et
    # ge_assets, voir enoc_mongo_service.fetch_genset_reference) : les 2
    # sources se recoupent en grande partie mais chacune couvre des sites que
    # l'autre manque, d'où l'union plutôt qu'une seule source.
    has_genset_snowflake = models.BooleanField(default=False, help_text="dg_count > 0 côté Snowflake.")
    has_genset_enoc = models.BooleanField(default=False, help_text="sites.nb_ge > 0 ou ge_assets INSTALLED côté ENOC.")
    nb_ge_enoc = models.IntegerField(null=True, blank=True, help_text="Nombre de GE déclaré côté ENOC (sites.nb_ge).")
    has_genset = models.BooleanField(default=False, db_index=True, help_text="has_genset_snowflake OU has_genset_enoc — seuls ces sites peuvent avoir une conso fuel.")

    # Snowflake — DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT (conso MESURÉE,
    # depuis le 2026-08 — remplace CONSUMPTION_FUEL, quasi vide). Un jour ne
    # compte que si QUALITY_STATUS='OK' ET VALID_POINT_COUNT>=2 ET
    # DROP_DETECTED=TRUE (voir fuel_consommation_snowflake.py).
    conso_snowflake_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    nb_jours_data = models.IntegerField(default=0, help_text="Nombre de jours du mois avec une valeur de consommation remontée.")

    # ESTIMATIONS (pas une mesure directe) déduites d'un delta de niveau de
    # cuve — deux sources indépendantes, gardées séparées (caveats différents) :
    #   - Snowflake TANK_LEVEL_AVG : alimentée en continu, 315 sites Sénégal
    #     avec GE couverts (07/08) — la plus fiable des deux.
    #   - ENOC fuel_level_readings : import historique ponctuel figé, 8 sites
    #     couverts (07/08) — voir enoc_mongo_service.fetch_estimated_consumption.
    # Dans les deux cas : niveau début - niveau fin + ravitaillements ENOC
    # entre les deux dates, calculé dans sync_fuel_consommation.py.
    conso_estimee_snowflake_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    conso_estimee_snowflake_nb_releves = models.IntegerField(null=True, blank=True)
    conso_estimee_enoc_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    conso_estimee_nb_releves = models.IntegerField(null=True, blank=True)

    # Conso spécifique — ratio pondéré mensuel SUM(conso_snowflake_l)/
    # SUM(ge_prod_kwh), PAS une moyenne de ratios journaliers (remplace
    # AVGSPECIFICFUELCONSO_L_KWH, abandonnée). ge_prod_kwh vient de
    # DB_GFMS_PROD.GOLD.GE_PROD_KWH. Vide si le site n'a pas de production
    # GE ce mois-là (conso mesurée conservée quand même).
    conso_specifique_moy_l_kwh = models.DecimalField(max_digits=12, decimal_places=6, null=True, blank=True)
    ge_prod_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Production GE du mois (DB_GFMS_PROD.GOLD.GE_PROD_KWH), utilisée pour la conso spécifique.")

    # Snowflake — DB_GFMS_PROD.GOLD.GFMS_FUEL_SENSOR_MONITORING_DATA (dernier statut connu du mois)
    sensor_status = models.CharField(max_length=32, null=True, blank=True)

    # Colonnes qualité VW_FUEL_REPORT — conservées pour audit (spec 2026-08),
    # agrégées sur le mois (SUM des compteurs journaliers, sauf quality_status
    # qui est la valeur du dernier jour du mois).
    quality_status = models.CharField(max_length=32, null=True, blank=True, help_text="QUALITY_STATUS VW_FUEL_REPORT du dernier jour du mois (OK / NO_VALID_LEVEL / LOW_QUALITY).")
    raw_point_count = models.IntegerField(null=True, blank=True)
    valid_point_count = models.IntegerField(null=True, blank=True)
    isolated_spike_count = models.IntegerField(null=True, blank=True)
    over_capacity_point_count = models.IntegerField(null=True, blank=True)
    refill_detected = models.BooleanField(default=False, help_text="Au moins un ravitaillement détecté sur le mois (REFILL_DETECTED VW_FUEL_REPORT).")
    estimated_refill_volume_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    # ENOC — agrégé depuis FuelEnocMovement sur le même mois/site
    enoc_qte_demandee_l = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    enoc_qte_validee_l = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    enoc_qte_ajoutee_l = models.DecimalField(max_digits=18, decimal_places=3, default=0)
    enoc_nb_demandes = models.IntegerField(default=0)

    # Jointure "concrète" : conso mesurée (capteur) vs quantité réellement ajoutée (ENOC)
    ecart_conso_vs_enoc_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    synced_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Consommation carburant mensuelle (automatisée)"
        verbose_name_plural = "Consommations carburant mensuelles (automatisées)"
        ordering = ["-year", "-month", "site_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["month_year", "site_id"],
                name="uniq_fuel_consommation_monthly_month_site",
            )
        ]
        indexes = [
            models.Index(fields=["month_year", "site_id"]),
            models.Index(fields=["year", "month"]),
        ]

    def __str__(self):
        return f"{self.month_year} · {self.site_id} · conso={self.conso_snowflake_l}"


class FuelConsommationSyncRun(models.Model):
    """
    Traçabilité des exécutions de sync_fuel_consommation (jointure Snowflake
    DB_GFMS_PROD.GOLD) — permet à l'UI d'afficher si la source Snowflake est
    "connectée" (dernière synchro réussie avec des lignes) et, en cas d'échec,
    le message d'erreur exact plutôt qu'un simple "0 lignes" ambigu.
    """

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    month_from = models.CharField(max_length=7, null=True, blank=True)
    month_to = models.CharField(max_length=7, null=True, blank=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)

    sites_fetched = models.IntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Fuel Consommation sync {self.month_from}→{self.month_to} [{self.status}]"


class FuelStockSnapshot(models.Model):
    """
    Stock carburant ACTUEL par site — contrairement à FuelConsommationMonthly
    (une somme sur un mois), le stock est un état à un instant T : une seule
    ligne par site (pas de clé mois), remplacée en totalité à chaque sync
    (voir sync_fuel_stock) plutôt qu'accumulée dans le temps.

    Jointure de 2 sources indépendantes, jamais fusionnées (même principe que
    Consommation — cf. spec 2026-08) :
      - Snowflake DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT : dernier relevé
        physiquement valide PAR SITE sur une fenêtre glissante de 30 jours
        (LAST_VALID_LEVEL/CAPACITY_L) — un jour calendaire fixe unique ne
        couvre que ~150/483 sites GE (vérifié le 2026-08), la fenêtre par
        site remonte à 182/483.
      - ENOC fuel_level_readings (import historique figé, MongoDB) : dernier
        relevé par site, mêmes garde-fous que l'estimation de conso
        (level_liters=0 + level_cm=None écarté comme valeur par défaut).
    """

    site_id = models.CharField(max_length=64, unique=True, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    country = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    typology = models.CharField(max_length=64, null=True, blank=True)
    site_type = models.CharField(max_length=64, null=True, blank=True)
    dg_count = models.CharField(max_length=16, null=True, blank=True)
    power_supply = models.CharField(max_length=64, null=True, blank=True)

    has_genset_snowflake = models.BooleanField(default=False)
    has_genset_enoc = models.BooleanField(default=False)
    nb_ge_enoc = models.IntegerField(null=True, blank=True)
    has_genset = models.BooleanField(default=False, db_index=True)

    # Snowflake — VW_FUEL_REPORT, dernier relevé valide (fenêtre 30j)
    stock_snowflake_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    capacity_snowflake_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    stock_snowflake_pct = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True, help_text="stock_snowflake_l / capacity_snowflake_l, en %.")
    stock_snowflake_date = models.DateField(null=True, blank=True, help_text="Date du relevé (peut être antérieure à aujourd'hui — dernier relevé disponible dans la fenêtre).")
    quality_status = models.CharField(max_length=32, null=True, blank=True)

    # ENOC — fuel_level_readings, dernier relevé (import historique figé)
    stock_enoc_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    stock_enoc_date = models.DateField(null=True, blank=True)

    synced_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Stock carburant (automatisé)"
        verbose_name_plural = "Stocks carburant (automatisés)"
        ordering = ["site_id"]
        indexes = [
            models.Index(fields=["has_genset"]),
        ]

    def __str__(self):
        return f"{self.site_id} · stock={self.stock_snowflake_l}"


class FuelStockSyncRun(models.Model):
    """Traçabilité des exécutions de sync_fuel_stock (même principe que FuelConsommationSyncRun)."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    sites_fetched = models.IntegerField(default=0)

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Fuel Stock sync [{self.status}]"