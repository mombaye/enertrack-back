from django.conf import settings
from django.db import models
from django.db.models import Q
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

    # NULL = absent à la source (jamais 0 par défaut).
    requested_quantity_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    approved_quantity_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    quantity_added_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    level_before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    level_before_unit = models.CharField(max_length=16, null=True, blank=True)

    level_after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    level_after_unit = models.CharField(max_length=16, null=True, blank=True)

    hour_meter_before = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    hour_meter_after = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)

    monthly_target_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    monthly_total_after_liters = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    target_percent_after = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
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
        colonnes prévues mais laissées vides tant que le format n'est pas défini) ;

    Le calcul CPH / consommation théorique / rapprochement stock n'est PAS
    stocké ici : il est calculé au grain site/jour, sur la plage exacte
    demandée, par fuel_tracking/services/cph_engine.py (voir FuelSiteDailyFacts).

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

    # Fichier métier validé ("Base août 26 validée", feuille "KPIs par site")
    # — 4e source, jamais fusionnée avec les colonnes Snowflake/ENOC/CPH
    # ci-dessus (même principe partout dans ce modèle) : une resynchro
    # Snowflake réécrirait typology/site_type à chaque cron, donc "le
    # fichier prime" s'applique en LECTURE (FuelConsommationListView.
    # serialize()), jamais en écrasant les champs Snowflake eux-mêmes.
    # conso_fichier_l/ge_runtime_fichier_h/ge_prod_fichier_kwh sont la
    # valeur ANNUELLE du fichier ÷ 12 (le fichier ne donne pas de détail
    # mensuel réel) — une moyenne annuelle répétée sur janvier→mois
    # courant, pas 12 vraies mesures mensuelles distinctes.
    zone_fichier = models.CharField(max_length=32, null=True, blank=True)
    typology_fichier = models.CharField(max_length=64, null=True, blank=True, help_text="Typologie réelle (Base GE.xlsx, colonne H).")
    typo_simple_fichier = models.CharField(max_length=64, null=True, blank=True, help_text="Typo simple (Base GE.xlsx, colonne I).")
    site_type_fichier = models.CharField(max_length=64, null=True, blank=True, help_text="On-Grid / Off-Grid (Base GE.xlsx, colonne M).")
    type_ge_fichier = models.CharField(max_length=160, null=True, blank=True, help_text="Type de GE — marque/modèle (Base GE.xlsx, colonne N).")
    batch_operationnel_fichier = models.CharField(max_length=64, null=True, blank=True)
    facturation_active_fichier = models.BooleanField(null=True, blank=True, help_text="Statut Facturation (Oui/Non) — mensuel, daté du mois en cours. Source la plus complète : fichier ESCO SN Facturation par site (3301 sites) ; à défaut Base août 26 (443 sites).")
    facturation_avec_ge_fichier = models.BooleanField(null=True, blank=True, help_text="Facturation avec GE (Oui/Non) — fichier ESCO SN Facturation par site, colonne 'Facturation avec GE oui|Non', déjà calculée par Ops (combine statut facturation + présence GE).")
    configuration_fichier = models.CharField(max_length=32, null=True, blank=True, help_text="Indoor / Outdoor — fichier ESCO SN Facturation par site (colonne Configuration v1, 3301 sites) ou Base août 26 (colonne Configuration, 443 sites) à défaut. Distinct de site_type_fichier (On-Grid/Off-Grid, vient de Base GE.xlsx).")
    load_fichier_w = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True, help_text="Load (load reelle used), en W.")
    conso_fichier_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Genset fuel conso [L/Month] du fichier (Base GE.xlsx, colonne P) — total déclaratif mensuel, distinct de conso_estimee_fichier_l (calcul CPH interne au fichier).")
    ge_runtime_fichier_h = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Durée de fonctionnement du GE en h, valeur du mois telle que fournie par le fichier (Base GE.xlsx, colonne X) — pas de ÷12, ce fichier donne déjà un total mensuel (contrairement à l'ancien fichier annuel).")
    ge_prod_fichier_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Genset Production [kWh/y] du fichier ÷ 12.")
    fichier_source = models.CharField(max_length=160, null=True, blank=True, help_text="Nom/version du fichier d'origine, traçabilité.")

    # Champs supplémentaires Base GE.xlsx (2026-08, colonnes O/T/V/Y/AF) —
    # valeurs brutes du fichier, conservées pour audit. Ils n'entrent dans
    # aucun calcul CPH (voir services/cph_engine.py).
    pge_kva_fichier = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Puissance GE (KVA), Base GE.xlsx colonne O.")
    ge_load_pct_fichier = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, help_text="GE load percentage en %, Base GE.xlsx colonne T.")
    cph_lph_fichier = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Cph en L/h, Base GE.xlsx colonne V.")
    conso_estimee_fichier_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Consommation de carburant en L, Base GE.xlsx colonne Y — renseigné pour seulement 5 des 469 lignes (valeurs 2,3,4,5,6h en colonne Running Time, motif manifestement factice/exemple) : conservé pour audit, mais PLUS utilisé pour la colonne affichée Conso estimée (voir conso_estimee_aout26_l).")
    conso_mesuree_fichier_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Fuel Consumption (L), Base GE.xlsx colonne AF — même limitation que conso_estimee_fichier_l (5/469 lignes). Conservé pour audit ; la colonne affichée Conso mesurée vue vient de Snowflake (conso_snowflake_l).")

    # "Base août 26 validée" (feuille KPIs par site) — 2e fichier, comble les
    # trous laissés par Base GE.xlsx sur running time/conso estimée (colonnes
    # X/Y quasi vides, 5/469 lignes réelles) avec une couverture complète sur
    # ses 443 sites (⊂ les 469 de Base GE.xlsx). Valeurs déjà mensuelles pour
    # conso_estimee_aout26_l (le fichier fournit directement L/y ET L/mois) ;
    # ge_runtime_aout26_h = Genset running time [hrs/yr] ÷ 12 (pas de
    # décomposition mensuelle dans le fichier).
    conso_estimee_aout26_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Genset fuel conso, valeur mensuelle déjà calculée dans le fichier (Base août 26 validée, colonne K).")
    ge_runtime_aout26_h = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Genset running time [hrs/yr] ÷ 12 (Base août 26 validée, colonne L).")

    # Relevés manuels des sociétés de gardiennage ("Synthèse Conso Fuel",
    # onglet "Synthese conso fuel") — 5e source, pour les sites SANS capteur
    # Snowflake (sensor_status != MONITORED) ni télémétrie GE exploitable.
    # Fichier mensuel distinct par nature (relevé physique de jauge par un
    # gardien, pas une mesure automatisée) : jamais fusionné avec
    # conso_snowflake_l, utilisé seulement en repli à l'affichage (voir
    # FuelConsommationListView.serialize()) quand Snowflake n'a rien.
    conso_gardien_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text='"Qtité cons considéréé (L)" du fichier de gardiennage — relevé de jauge validé par le gardien/technicien, retenu pour le mois.')
    gardien_statut = models.CharField(max_length=32, null=True, blank=True, help_text='"Statut Conso Fuel" du fichier (OK / OK SOUS RESERVE).')
    gardien_date_releve_finale = models.CharField(max_length=32, null=True, blank=True, help_text="Date de Relevé Finale telle que fournie par le fichier (texte brut, formats mixtes) — vide si la période n'était pas encore clôturée à l'export du fichier.")
    gardien_source = models.CharField(max_length=200, null=True, blank=True, help_text="Nom/onglet du fichier de gardiennage d'origine, traçabilité.")

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


class FuelRapprochementThreshold(models.Model):
    """
    Seuils de déclenchement des statuts de rapprochement stock (spec C).
    Configurables via l'Admin Django sans redéploiement.
    Le jeu 'default' est le jeu actif ; s'il n'existe pas, les valeurs
    par défaut de services/cph_engine.py (max(100 L, 10 %) / max(200 L, 20 %))
    s'appliquent.
    """
    label = models.CharField(
        max_length=64, default="default", unique=True,
        help_text="Identifiant du jeu de seuils ('default' = jeu actif)."
    )
    seuil_ok_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=10,
        help_text="Écart ≤ max(seuil_ok_l, seuil_ok_pct% × conso_ref) → statut OK."
    )
    seuil_a_justifier_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=20,
        help_text="Écart ≤ max(seuil_aj_l, seuil_a_justifier_pct% × conso_ref) → A_JUSTIFIER ; au-delà → A_INVESTIGUER."
    )
    seuil_ok_l = models.DecimalField(
        max_digits=10, decimal_places=2, default=100,
        help_text="Plancher absolu (L) pour le seuil OK : max(ce seuil, seuil_ok_pct% × conso_ref)."
    )
    seuil_aj_l = models.DecimalField(
        max_digits=10, decimal_places=2, default=200,
        help_text="Plancher absolu (L) pour le seuil A_JUSTIFIER : max(ce seuil, seuil_a_justifier_pct% × conso_ref)."
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Seuil de rapprochement carburant"
        verbose_name_plural = "Seuils de rapprochement carburant"

    def __str__(self):
        return f"Seuils rapprochement [{self.label}] OK≤max({self.seuil_ok_l}L,{self.seuil_ok_pct}%) AJ≤max({self.seuil_aj_l}L,{self.seuil_a_justifier_pct}%)"


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

    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    country = models.CharField(max_length=64, null=True, blank=True, db_index=True)

    # NULL = snapshot courant (remplacé à chaque sync) ; valeur = snapshot mensuel archivé.
    snapshot_year = models.IntegerField(null=True, blank=True, db_index=True,
        help_text="NULL = snapshot courant ; valeur = snapshot mensuel (année).")
    snapshot_month = models.IntegerField(null=True, blank=True,
        help_text="NULL = snapshot courant ; valeur = snapshot mensuel (mois 1-12).")

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
        constraints = [
            models.UniqueConstraint(
                fields=["site_id"],
                condition=Q(snapshot_year__isnull=True),
                name="fuel_stock_snapshot_current_uniq",
            ),
            models.UniqueConstraint(
                fields=["site_id", "snapshot_year", "snapshot_month"],
                condition=Q(snapshot_year__isnull=False),
                name="fuel_stock_snapshot_monthly_uniq",
            ),
        ]
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


# ─────────────────────────────────────────────────────────────────────────────
# Suivi Carburant / CPH — instruction globale validée (abaque PRP 50 Hz).
# Le calcul lui-même vit dans services/cph_engine.py : ces modèles ne stockent
# que le référentiel (abaque, mappage inventaire), les faits bruts Snowflake au
# grain site/jour et le fichier d'observation — jamais un résultat calculé,
# pour que toute plage de dates choisie soit recalculée exactement.
# ─────────────────────────────────────────────────────────────────────────────


class CphCurve(models.Model):
    """Courbe CPH PRP 50 Hz (feuille « Abaque CPH »). CPH (L/h) = a·x² + b·x + c, x = charge."""

    class Status(models.TextChoices):
        VALIDE_CONSTRUCTEUR = "VALIDÉ_CONSTRUCTEUR", "Validée constructeur"
        HISTORIQUE_A_VALIDER = "HISTORIQUE_A_VALIDER", "Historique à valider"
        FICHE_ARCHIVEE_A_VALIDER = "FICHE_ARCHIVEE_A_VALIDER", "Fiche archivée à valider"
        FICHE_DISTRIBUTEUR_A_VALIDER = "FICHE_DISTRIBUTEUR_A_VALIDER", "Fiche distributeur à valider"

    curve_id = models.CharField(max_length=16, unique=True)
    manufacturer = models.CharField(max_length=64)
    model = models.CharField(max_length=64)
    model_key = models.CharField(max_length=64, db_index=True)
    variant = models.CharField(max_length=128, blank=True)
    prp_kva = models.DecimalField(max_digits=10, decimal_places=2)
    prp_kw = models.DecimalField(max_digits=10, decimal_places=2)
    power_factor = models.DecimalField(
        max_digits=4, decimal_places=3, null=True, blank=True,
        help_text="cos φ du type GE (abaque) — configurable : sert au plafond 105 % × kVA × cos φ. Absent → courbe inutilisable.",
    )
    voltage_v = models.IntegerField(null=True, blank=True)
    phases = models.IntegerField(null=True, blank=True)
    frequency = models.CharField(max_length=64, blank=True)
    regime = models.CharField(max_length=64, blank=True)
    conso_25_l_h = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, help_text="Point extrapolé, informatif.")
    conso_50_l_h = models.DecimalField(max_digits=10, decimal_places=3)
    conso_75_l_h = models.DecimalField(max_digits=10, decimal_places=3)
    conso_100_l_h = models.DecimalField(max_digits=10, decimal_places=3)
    coef_a = models.DecimalField(max_digits=12, decimal_places=6)
    coef_b = models.DecimalField(max_digits=12, decimal_places=6)
    coef_c = models.DecimalField(max_digits=12, decimal_places=6)
    domain = models.CharField(max_length=128, blank=True)
    status = models.CharField(max_length=32, choices=Status.choices, db_index=True)
    source = models.CharField(max_length=255, blank=True)
    source_url = models.URLField(max_length=500, blank=True)
    note = models.TextField(blank=True)

    business_approved = models.BooleanField(
        default=False,
        help_text="Activation métier explicite d'une courbe non VALIDÉ_CONSTRUCTEUR (historique, archivée, distributeur).",
    )
    business_approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    business_approved_at = models.DateTimeField(null=True, blank=True)
    business_approval_comment = models.TextField(blank=True)

    abaque_file = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Courbe CPH PRP 50 Hz"
        verbose_name_plural = "Courbes CPH PRP 50 Hz"
        ordering = ["curve_id"]

    def __str__(self):
        return f"{self.curve_id} {self.manufacturer} {self.model} ({self.prp_kva} kVA PRP) [{self.status}]"

    @property
    def is_usable(self) -> bool:
        return self.status == self.Status.VALIDE_CONSTRUCTEUR or self.business_approved


class CphInventoryMapping(models.Model):
    """
    (Libellé GE, kVA) de l'inventaire (Base GE, « Type de GE » / « Puissance GE
    KVA ») → courbe CPH (feuille « Mappage inventaire », clé libellé + kVA). Une courbe n'est appliquée à un site qu'après
    validation métier explicite (plaque signalétique) : validated_curve.
    """

    class AbaqueStatus(models.TextChoices):
        CANDIDAT_UNIQUE_A_VALIDER = "CANDIDAT_UNIQUE_A_VALIDER", "Candidat unique à valider"
        COURBE_CPH_MANQUANTE = "COURBE_CPH_MANQUANTE", "Courbe CPH manquante"
        MODELE_AMBIGU = "MODELE_AMBIGU", "Modèle ambigu"

    inventory_label = models.CharField(max_length=160)
    inventory_label_normalized = models.CharField(max_length=160, db_index=True)
    inventory_kva = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    site_count = models.IntegerField(null=True, blank=True)
    normalized_key = models.CharField(max_length=64, blank=True)
    candidate_curve_ids = models.JSONField(default=list, blank=True)
    candidate_models = models.TextField(blank=True)
    abaque_status = models.CharField(max_length=32, choices=AbaqueStatus.choices)
    action_required = models.TextField(blank=True)

    class MatchStatus(models.TextChoices):
        AUTO_VALIDE_COMPATIBLE = "AUTO_VALIDE_COMPATIBLE", "Auto-validé (compatible)"
        VALIDE_MANUELLEMENT = "VALIDE_MANUELLEMENT", "Validé manuellement"
        A_VALIDER = "A_VALIDER", "À valider"
        COURBE_CPH_MANQUANTE = "COURBE_CPH_MANQUANTE", "Courbe CPH manquante"
        MODELE_AMBIGU = "MODELE_AMBIGU", "Modèle ambigu"
        REJETE = "REJETE", "Rejeté"

    # Statut de CORRESPONDANCE plaque → courbe (services/cph_matching.py), distinct du
    # statut de qualité de la courbe (CphCurve.status), qui n'est jamais modifié ici.
    match_status = models.CharField(max_length=32, choices=MatchStatus.choices, default=MatchStatus.A_VALIDER, db_index=True)
    match_score = models.IntegerField(null=True, blank=True, help_text="Score de compatibilité 0-100 du candidat unique.")
    match_method = models.CharField(max_length=96, blank=True, help_text="AUTO:<critères> | MANUEL | RETRAIT_MANUEL")
    match_reasons = models.JSONField(default=list, blank=True)
    matched_at = models.DateTimeField(null=True, blank=True)

    # Courbe appliquée (auto ou manuelle) ; validated_by renseigné seulement pour un choix humain.
    validated_curve = models.ForeignKey(CphCurve, null=True, blank=True, on_delete=models.PROTECT, related_name="validated_mappings")
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_comment = models.TextField(blank=True)

    abaque_file = models.CharField(max_length=255, blank=True)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Mappage inventaire GE → courbe CPH"
        verbose_name_plural = "Mappages inventaire GE → courbe CPH"
        ordering = ["inventory_label", "inventory_kva"]
        constraints = [models.UniqueConstraint(fields=["inventory_label", "inventory_kva"], name="uniq_cph_mapping_label_kva")]

    def __str__(self):
        return f"{self.inventory_label} ({self.inventory_kva} kVA) → {self.validated_curve_id or self.abaque_status}"


class CphMappingHistory(models.Model):
    """Historique des correspondances plaque → courbe : chaque changement (auto, manuel, import)."""

    mapping = models.ForeignKey(CphInventoryMapping, null=True, on_delete=models.SET_NULL, related_name="history")
    inventory_label = models.CharField(max_length=160)
    inventory_kva = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    old_status = models.CharField(max_length=32, blank=True)
    new_status = models.CharField(max_length=32)
    old_curve_id = models.CharField(max_length=16, blank=True)
    new_curve_id = models.CharField(max_length=16, blank=True)
    score = models.IntegerField(null=True, blank=True)
    rule = models.CharField(max_length=160, blank=True, help_text="Règle / méthode appliquée")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    comment = models.TextField(blank=True)

    class Meta:
        verbose_name = "Historique correspondance plaque → courbe"
        verbose_name_plural = "Historique correspondances plaque → courbe"
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"{self.inventory_label} : {self.old_status or '∅'} → {self.new_status} ({self.changed_at:%Y-%m-%d})"


class FuelSiteInventory(models.Model):
    """Snapshot DB_GFMS_ANALYTICS_PROD.GOLD.SITE_ESCO_CURRENT — clé (country, data_id)."""

    country = models.CharField(max_length=64, db_index=True)
    data_id = models.BigIntegerField()
    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    grid_supply = models.CharField(max_length=64, null=True, blank=True, help_text="SITE_ESCO_CURRENT.GRID_SUPPLY_MODIFIED (brut).")
    dg_count = models.IntegerField(null=True, blank=True)
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Inventaire site (Snowflake)"
        verbose_name_plural = "Inventaire sites (Snowflake)"
        constraints = [models.UniqueConstraint(fields=["country", "data_id"], name="uniq_fuel_inventory_country_data_id")]


class FuelSiteDailyFacts(models.Model):
    """
    Faits bruts Snowflake (lecture seule) au grain (country, data_id, date).
    Aucune décision ici : pas de COALESCE à 0, pas de source retenue — une
    mesure absente reste NULL. Le moteur (services/cph_engine.py) calcule
    disponibilités, source retenue et CPH sur la plage exacte demandée.
    """

    country = models.CharField(max_length=64)
    data_id = models.BigIntegerField()
    site_id = models.CharField(max_length=64, db_index=True)
    date = models.DateField(db_index=True)

    # GENSET_REPORT (valeurs brutes, bornes 0-24 h contrôlées par le moteur)
    dse_runtime_h = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, help_text="DG_RUNTIME_CONTROLLER brut (h).")
    dg_on_runtime_h = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, help_text="DG_RUNTIME_CALCULATED brut — « Day DG On » (h).")
    dg_production_kwh = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, help_text="DG_PRODUCTION_KWH brut (kWh).")

    # GFMS_DATA_TRACKER_NC — compteur horaire, intervalles continus et plausibles
    tracker_runtime_h = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Σ incréments DG_TOTAL_RUNNING_TIME_MINUTES sur intervalles plausibles ÷ 60 (h).")
    tracker_covered_min = models.IntegerField(null=True, blank=True, help_text="Minutes couvertes par des intervalles continus et plausibles.")
    tracker_ge_on_slots = models.IntegerField(null=True, blank=True, help_text="Créneaux 5 min avec incrément compteur > 0.")

    # RECTIFIER_EFFICIENCY_STATUS — agrégé par créneaux de 5 minutes
    rectifier_slots = models.IntegerField(null=True, blank=True)
    rectifier_active_slots = models.IntegerField(null=True, blank=True, help_text="Créneaux 5 min au statut redresseur actif.")
    p_dc_ge_tracker_kw = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text="Moyenne P_DC (kW) sur les créneaux GE en marche (compteur tracker).")
    eff_ge_tracker = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True, help_text="Rendement redresseur moyen (0-1] sur ces mêmes créneaux.")
    p_dc_rect_active_kw = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text="Moyenne P_DC (kW) sur les créneaux redresseur actif.")
    eff_rect_active = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    p_dc_day_kw = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True, help_text="Moyenne P_DC (kW) sur la journée.")
    eff_day = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)

    # AC_METER — instrument AC (indoor)
    ac_active_power_avg_w = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True, help_text="ACT_ACTIVE_POWER_AVG brut (W).")
    ac_energy_p = models.DecimalField(max_digits=16, decimal_places=4, null=True, blank=True, help_text="ACT_ENERGY_P brut.")
    ac_point_count = models.IntegerField(null=True, blank=True, help_text="Nombre de mesures ACT_ACTIVE_POWER_AVG non nulles du jour.")

    # VW_FUEL_REPORT (DB_GFMS_ANALYTICS_DEV) — conso MESURÉE du jour, même filtre strict que la conso
    # mensuelle : QUALITY_STATUS = 'OK' et VALID_POINT_COUNT ≥ 2 ; baisse détectée → volume de la baisse,
    # aucune baisse ni remplissage → 0 L mesuré ; sinon NULL (jour non mesurable, jamais 0).
    measured_conso_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Conso mesurée du jour (L), VW_FUEL_REPORT.")
    fuel_raw_points = models.IntegerField(null=True, blank=True, help_text="RAW_POINT_COUNT du jour (VW_FUEL_REPORT).")

    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Faits journaliers carburant (Snowflake)"
        verbose_name_plural = "Faits journaliers carburant (Snowflake)"
        constraints = [models.UniqueConstraint(fields=["country", "data_id", "date"], name="uniq_fuel_daily_facts_key")]
        indexes = [models.Index(fields=["site_id", "date"])]


class FuelDailyFactsSyncRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "RUNNING", "En cours"
        SUCCESS = "SUCCESS", "Succès"
        FAILED = "FAILED", "Échec"

    date_from = models.DateField()
    date_to = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING, db_index=True)
    rows_written = models.IntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class FuelObservationImport(models.Model):
    """Traçabilité d'un import du fichier d'observation standard."""

    file_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    uploaded_at = models.DateTimeField(default=timezone.now)
    rule_version = models.CharField(max_length=64)
    rows_total = models.IntegerField(default=0)
    rows_imported = models.IntegerField(default=0)
    rows_rejected = models.IntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-uploaded_at"]


class FuelObservation(models.Model):
    """Ligne du fichier d'observation — une cellule vide reste NULL, jamais 0."""

    obs_import = models.ForeignKey(FuelObservationImport, on_delete=models.CASCADE, related_name="rows")
    country = models.CharField(max_length=64, null=True, blank=True)
    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    observation_start = models.DateField()
    observation_end = models.DateField()
    opening_fuel_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    closing_fuel_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuel_deliveries_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuel_transfer_in_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuel_transfer_out_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuel_theft_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    fuel_drain_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    observation_status = models.CharField(max_length=64, null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    justificatif = models.TextField(null=True, blank=True)

    class Meta:
        verbose_name = "Observation stock carburant"
        verbose_name_plural = "Observations stock carburant"
        indexes = [models.Index(fields=["site_id", "observation_start", "observation_end"])]


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Registre unifié de stock et de mouvements carburant (Suivis Stock).
# Sources : ENOC (connecteur existant, via FuelEnocMovement), fichiers des
# sociétés de gardiennage (Loliforce, SCSS), Closing mensuel (stocks et mouvements de période) et
# historique refueling. Règles : une donnée absente reste NULL (jamais 0),
# aucun relevé ni mouvement n'est supprimé physiquement, toute correction est
# tracée (FuelRegistryCorrection). Voir docs/fuel_stock_registry.md.
# ─────────────────────────────────────────────────────────────────────────────


class FuelImportBatch(models.Model):
    """Un fichier reçu (répertoire monté FUEL_IMPORT_ROOT ou upload) — fichier original jamais modifié."""

    class SourceType(models.TextChoices):
        ENOC = "ENOC", "ENOC"
        LOLIFORCE = "LOLIFORCE", "Loliforce — relevés de stock terrain"
        SCSS = "SCSS", "SCSS — relevés de stock terrain"
        CLOSING = "CLOSING", "Closing mensuel — stocks et mouvements de période"
        HISTORICAL_REFUELING = "HISTORICAL_REFUELING", "Historique refueling — source historique"
        TANK_MODELS = "TANK_MODELS", "Modèles de cuves — calculateur et conversion cm vers litres"

    class Status(models.TextChoices):
        RECEIVED = "RECEIVED", "Reçu"
        PROCESSING = "PROCESSING", "En traitement"
        VALIDATION_REQUIRED = "VALIDATION_REQUIRED", "Validation requise"
        ACCEPTED = "ACCEPTED", "Accepté"
        REJECTED = "REJECTED", "Rejeté"
        FAILED = "FAILED", "Échec technique"

    source_type = models.CharField(max_length=32, choices=SourceType.choices, db_index=True)
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True, help_text="SHA-256 du fichier original.")
    file_size = models.BigIntegerField(null=True, blank=True)
    stored_path = models.CharField(max_length=1024, null=True, blank=True, help_text="Emplacement actuel du fichier (accepted/rejected).")
    archive_path = models.CharField(max_length=1024, null=True, blank=True, help_text="Copie d'archive immuable.")
    report_path = models.CharField(max_length=1024, null=True, blank=True)
    origin = models.CharField(max_length=16, default="FOLDER", help_text="FOLDER (répertoire monté), UPLOAD (interface), ENOC (connecteur).")
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    status_message = models.TextField(null=True, blank=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")
    relaunch_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="relaunches")
    relaunch_reason = models.TextField(null=True, blank=True)
    rows_total = models.IntegerField(default=0)
    rows_accepted = models.IntegerField(default=0)
    rows_warning = models.IntegerField(default=0)
    rows_rejected = models.IntegerField(default=0)
    rows_pending = models.IntegerField(default=0)
    rows_duplicate = models.IntegerField(default=0)
    summary = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    declared_period_start = models.DateField(null=True, blank=True, help_text="Première date de relevé / d'événement du fichier.")
    declared_period_end = models.DateField(null=True, blank=True, help_text="Dernière date de relevé / d'événement du fichier.")
    mapping_version = models.CharField(max_length=64, null=True, blank=True, help_text="Version du mapping de colonnes appliqué (docs/fuel_stock_registry.md).")

    class Meta:
        db_table = "fuel_import_batch"
        ordering = ["-received_at"]
        verbose_name = "Lot d'import carburant"
        verbose_name_plural = "Lots d'import carburant"


class FuelImportRow(models.Model):
    """Ligne brute d'un lot — chargée telle quelle AVANT toute interprétation."""

    class ValidationStatus(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Acceptée"
        WARNING = "WARNING", "Acceptée avec alerte"
        REJECTED = "REJECTED", "Rejetée"
        PENDING = "PENDING", "En attente de validation"

    batch = models.ForeignKey(FuelImportBatch, on_delete=models.PROTECT, related_name="rows")
    sheet_name = models.CharField(max_length=128, null=True, blank=True)
    row_number = models.IntegerField()
    raw_payload = models.JSONField(default=dict)
    site_id_raw = models.CharField(max_length=128, null=True, blank=True)
    site_id_resolved = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    event_datetime = models.DateTimeField(null=True, blank=True)
    validation_status = models.CharField(max_length=16, choices=ValidationStatus.choices, default=ValidationStatus.PENDING, db_index=True)
    messages = models.JSONField(default=list, blank=True, help_text="[{code, message}] — contrôles appliqués à la ligne.")
    row_fingerprint = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "fuel_import_row"
        ordering = ["batch_id", "sheet_name", "row_number"]
        indexes = [models.Index(fields=["batch", "validation_status"])]


class FuelTankModel(models.Model):
    """
    Bibliothèque des MODÈLES de cuves (calculateur universel de Steve, onglet « Tank DB ») : formes, cotes,
    profils de fond, capacité de plaque. Un modèle n'est PAS une cuve installée sur un site.
    """

    class Status(models.TextChoices):
        COMPLET = "COMPLET", "Complet"
        A_CONFIRMER = "A_CONFIRMER", "À confirmer (écart plaque / géométrie > 5 %)"
        INCOMPLET = "INCOMPLET", "Incomplet (forme ou cotes insuffisantes)"

    tank_model_id = models.CharField(max_length=64, unique=True)
    country = models.CharField(max_length=64, null=True, blank=True)
    source_file = models.CharField(max_length=255, null=True, blank=True)
    source_sheet = models.CharField(max_length=128, null=True, blank=True)
    source_row = models.IntegerField(null=True, blank=True)
    source_label = models.CharField(max_length=255, null=True, blank=True, help_text="Libellé « Type / Shape » du fichier.")
    shape_code = models.CharField(max_length=16, null=True, blank=True, help_text="Famille du calculateur : h-circ, h-elli, h-oblo, v-cyl, v-tronc, rect.")
    shape_label = models.CharField(max_length=128, null=True, blank=True)
    tank_family = models.CharField(max_length=128, null=True, blank=True)
    nominal_capacity_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Capacité de plaque.")
    dimension_1_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimension_2_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimension_3_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    bottom_profile = models.CharField(max_length=16, null=True, blank=True)
    bottom_z_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    top_profile = models.CharField(max_length=16, null=True, blank=True)
    top_z_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    geometric_full_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    calibration_k = models.DecimalField(max_digits=10, decimal_places=6, null=True, blank=True)
    plate_gap_pct = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, help_text="(géométrie − plaque) / plaque, en %.")
    geometric_formula_version = models.CharField(max_length=64, null=True, blank=True)
    calibration_formula_version = models.CharField(max_length=64, null=True, blank=True)
    calibration_method = models.CharField(max_length=128, null=True, blank=True)
    model_status = models.CharField(max_length=16, choices=Status.choices, default=Status.INCOMPLET, db_index=True)
    status_reason = models.TextField(null=True, blank=True)
    import_batch = models.ForeignKey(FuelImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_tank_model"
        ordering = ["tank_model_id"]


class FuelTankMaster(models.Model):
    """
    Cuve RÉELLEMENT présente sur un site (table fuel_site_tank) : forme et cotes observées, capacité de plaque,
    modèle de la bibliothèque s'il est connu, preuve. Utilisée pour cm → L uniquement si elle est VALIDÉE.
    """

    class Shape(models.TextChoices):
        HORIZONTAL_CYLINDER = "HORIZONTAL_CYLINDER", "Cylindre horizontal"
        VERTICAL_CYLINDER = "VERTICAL_CYLINDER", "Cylindre vertical"
        RECTANGULAR = "RECTANGULAR", "Parallélépipède"
        HORIZONTAL_ELLIPTICAL = "HORIZONTAL_ELLIPTICAL", "Cylindre elliptique horizontal"
        CUSTOM_CURVE = "CUSTOM_CURVE", "Abaque (courbe de jaugeage)"

    class ValidationStatus(models.TextChoices):
        VALIDEE = "VALIDEE", "Validée"
        PROPOSEE = "PROPOSEE", "Proposée"
        A_CONFIRMER = "A_CONFIRMER", "À confirmer"
        INCOMPLETE = "INCOMPLETE", "Incomplète"
        PLUSIEURS_CUVES_A_IDENTIFIER = "PLUSIEURS_CUVES_A_IDENTIFIER", "Plusieurs cuves à identifier"
        REJETEE = "REJETEE", "Rejetée"

    class ModelMatch(models.TextChoices):
        NONE = "NONE", "Aucun modèle"
        PROPOSED = "PROPOSED", "Modèle proposé"
        CONFIRMED = "CONFIRMED", "Modèle confirmé"
        REJECTED = "REJECTED", "Modèle écarté"

    site_id = models.CharField(max_length=64, db_index=True)
    tank_id = models.CharField(max_length=32, help_text="Identifiant de la cuve sur le site (1, 2…). Jamais déduit automatiquement d'un relevé.")
    tank_model = models.ForeignKey(FuelTankModel, null=True, blank=True, on_delete=models.PROTECT, related_name="site_tanks")
    model_match_status = models.CharField(max_length=16, choices=ModelMatch.choices, default=ModelMatch.NONE)
    shape = models.CharField(max_length=32, choices=Shape.choices, null=True, blank=True, help_text="tank_shape_observed (forme relevée).")
    shape_family = models.CharField(max_length=16, null=True, blank=True, help_text="Famille du calculateur (h-circ, rect…).")
    dimension_1_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimension_2_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimension_3_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    bottom_profile = models.CharField(max_length=16, null=True, blank=True, help_text="Profil des fonds (plat si inconnu dans la source).")
    bottom_z_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    top_profile = models.CharField(max_length=16, null=True, blank=True)
    top_z_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    length_m = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    width_m = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    height_m = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    diameter_m = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)
    capacity_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Capacité déclarée par la source (le Closing la recalcule depuis les cotes).")
    capacity_plate_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Capacité de plaque (sert au volume calibré).")
    capacity_plate_source = models.CharField(max_length=255, null=True, blank=True)
    compat_score = models.IntegerField(null=True, blank=True, help_text="Score de compatibilité 0-100 (forme + cotes + modèle + capacité) : "
                                                                    "autorise l'estimation d'une cuve PROPOSÉE au-dessus du seuil configurable.")
    serial_number = models.CharField(max_length=128, null=True, blank=True)
    number_of_tanks_on_site = models.IntegerField(null=True, blank=True)
    calibration_curve = models.JSONField(null=True, blank=True, help_text="[[niveau_cm, volume_L], …] croissant.")
    calibration_validated = models.BooleanField(default=False)
    source_type = models.CharField(max_length=32, default="MANUAL", help_text="CLOSING, HISTORICAL_REFUELING, ENOC, TERRAIN ou MANUAL.")
    source_batch = models.ForeignKey(FuelImportBatch, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_file = models.CharField(max_length=255, null=True, blank=True)
    source_sheet = models.CharField(max_length=128, null=True, blank=True)
    source_row = models.IntegerField(null=True, blank=True)
    proof_reference = models.CharField(max_length=512, null=True, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    validation_status = models.CharField(max_length=32, choices=ValidationStatus.choices, default=ValidationStatus.INCOMPLETE, db_index=True)
    validation_reason = models.TextField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    validated_at = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)
    comment = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_site_tank"
        ordering = ["site_id", "tank_id"]
        constraints = [models.UniqueConstraint(fields=["site_id", "tank_id"], name="uniq_fuel_tank_site_tank")]


class FuelGuardScope(models.Model):
    """Périmètre de gardiennage configuré : quel prestataire relève quel site, sur quelle période."""

    class Provider(models.TextChoices):
        LOLIFORCE = "LOLIFORCE", "Loliforce"
        SCSS = "SCSS", "SCSS"

    site_id = models.CharField(max_length=64, db_index=True)
    provider = models.CharField(max_length=16, choices=Provider.choices, db_index=True)
    active_from = models.DateField()
    active_to = models.DateField(null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "fuel_guard_scope"
        ordering = ["site_id", "-active_from"]


class FuelStockObservation(models.Model):
    """Relevé de niveau de cuve, quelle qu'en soit la source. Jamais supprimé ; corrections tracées."""

    class SourceType(models.TextChoices):
        ENOC = "ENOC", "ENOC"
        LOLIFORCE = "LOLIFORCE", "Loliforce"
        SCSS = "SCSS", "SCSS"
        SNOWFLAKE = "SNOWFLAKE", "Snowflake (information technique)"
        CLOSING = "CLOSING", "Closing"

    class Unit(models.TextChoices):
        L = "L", "Litres"
        CM = "CM", "Centimètres"

    class ConversionMethod(models.TextChoices):
        DIRECT_LITERS = "DIRECT_LITERS", "Relevé direct en litres"
        TANK_CURVE = "TANK_CURVE", "Abaque cuve validé"
        GEOMETRIC_CALCULATION = "GEOMETRIC_CALCULATION", "Calcul géométrique (dimensions validées)"
        CALIBRATED = "CALIBRATED", "Volume calibré (géométrie × capacité de plaque)"
        NONE = "NONE", "Aucune"

    class ConversionStatus(models.TextChoices):
        VALID = "VALID", "Valide"
        GEOMETRIC_ONLY = "GEOMETRIC_ONLY", "Volume géométrique seulement (capacité de plaque à confirmer)"
        PENDING_TANK_MAPPING = "PENDING_TANK_MAPPING", "Cuve à référencer / valider"
        INVALID = "INVALID", "Invalide"

    class ValidationStatus(models.TextChoices):
        PENDING = "PENDING", "À traiter"
        VALIDATED = "VALIDATED", "Validé (auto ou manuel)"
        REJECTED = "REJECTED", "Rejeté"
        NON_EXPLOITABLE = "NON_EXPLOITABLE", "Non exploitable (conservé)"

    source_type = models.CharField(max_length=16, choices=SourceType.choices, db_index=True)
    source_event_id = models.CharField(max_length=128, help_text="Identifiant de l'événement source (ENOC : request_code:before|after ; fichier : empreinte).")
    batch = models.ForeignKey(FuelImportBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="observations")
    import_row = models.ForeignKey(FuelImportRow, null=True, blank=True, on_delete=models.PROTECT, related_name="observations")
    site_id = models.CharField(max_length=64, db_index=True)
    tank_id = models.CharField(max_length=32, null=True, blank=True)
    observation_datetime = models.DateTimeField(db_index=True)
    level_raw = models.CharField(max_length=128, null=True, blank=True, help_text="Valeur telle que saisie à la source.")
    level_value = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    level_unit = models.CharField(max_length=4, choices=Unit.choices, null=True, blank=True)
    level_liters = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    conversion_method = models.CharField(max_length=24, choices=ConversionMethod.choices, default=ConversionMethod.NONE)
    conversion_status = models.CharField(max_length=24, choices=ConversionStatus.choices, default=ConversionStatus.PENDING_TANK_MAPPING, db_index=True)
    conversion_message = models.TextField(null=True, blank=True)
    capacity_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Capacité de la (des) cuve(s) validée(s) au moment de la conversion.")
    volume_geometric_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Volume géométrique (valeur technique).")
    volume_calibrated_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Volume calibré sur la capacité de plaque.")
    declared_liters = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Litres déclarés par la source (valeur brute).")
    level_cm = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, help_text="Niveau en cm relevé (même si des litres sont aussi déclarés).")
    cm_liters_gap_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Écart litres déclarés − conversion du niveau cm.")
    fill_pct = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    conversion_rule = models.CharField(max_length=255, null=True, blank=True)
    tank_model_ref = models.CharField(max_length=64, null=True, blank=True)
    issue_codes = models.JSONField(default=list, blank=True, help_text="Codes des contrôles en échec (pilotent l'action à faire).")
    hour_meter = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    source_user = models.CharField(max_length=255, null=True, blank=True)
    source_reference = models.CharField(max_length=255, null=True, blank=True)
    guard_scope_status = models.CharField(max_length=24, null=True, blank=True, help_text="IN_SCOPE, NOT_CONFIGURED, PROVIDER_MISMATCH (sources gardiennage).")
    # MVP : qualité du relevé et estimation (jamais présentée comme une valeur certifiée)
    reading_quality_status = models.CharField(max_length=40, null=True, blank=True, db_index=True,
                                              help_text="LITRE_DIRECT, CM_CONVERTI_CUVE_VALIDEE, CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE, "
                                                        "CM_CONVERTI_CUVE_PROPOSEE_RESERVE, CM_SANS_CUVE, UNITE_INCONNUE, VALEUR_INVALIDE, DOUBLON_POTENTIEL.")
    confidence = models.CharField(max_length=8, null=True, blank=True, help_text="HAUTE / MOYENNE / FAIBLE.")
    estimated_liters = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True,
                                           help_text="Litres ESTIMÉS (cuve géométrique sans plaque / cuve proposée) : jamais level_liters.")
    estimation_mode = models.CharField(max_length=40, null=True, blank=True)
    estimation_detail = models.JSONField(default=dict, blank=True, help_text="Cuve, statut, modèle, score, dimensions, formule et version.")
    source_context = models.JSONField(default=dict, blank=True,
                                      help_text="Contexte fourni par la source (ex. totaux de période du Closing : livré, rajouts/retraits, volé, vidangé, conso).")
    potential_duplicate_of = models.IntegerField(null=True, blank=True, db_index=True,
                                                 help_text="Relevé de priorité supérieure aux valeurs équivalentes (même site, jour et cuve) : DOUBLON_POTENTIEL.")
    source_provenance = models.CharField(max_length=32, null=True, blank=True, help_text="ENOC : provenance du statut brut.")
    business_status = models.CharField(max_length=28, null=True, blank=True, db_index=True, help_text="ENOC : statut métier eGrid.")
    status_mapping_rule = models.CharField(max_length=255, null=True, blank=True)
    eligible_for_stock = models.BooleanField(null=True, blank=True, help_text="ENOC : statut éligible au stock (NULL hors ENOC).")
    comment = models.TextField(null=True, blank=True)
    proof_reference = models.CharField(max_length=512, null=True, blank=True)
    validation_status = models.CharField(max_length=16, choices=ValidationStatus.choices, default=ValidationStatus.PENDING, db_index=True)
    validation_reason = models.TextField(null=True, blank=True)
    review_flags = models.JSONField(default=list, blank=True, help_text="Motifs de revue indépendants de la conversion (conflit même source, demande ENOC non clôturée…).")
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_stock_observation"
        ordering = ["-observation_datetime"]
        constraints = [models.UniqueConstraint(fields=["source_type", "source_event_id"], name="uniq_fuel_stock_obs_source_event")]
        indexes = [
            models.Index(fields=["site_id", "observation_datetime"]),
            models.Index(fields=["validation_status", "conversion_status"]),
        ]


class FuelMovement(models.Model):
    """Mouvement de carburant (livraison, appoint, transfert, retrait, vol, vidange, correction)."""

    class SourceType(models.TextChoices):
        ENOC = "ENOC", "ENOC"
        HISTORICAL_REFUELING = "HISTORICAL_REFUELING", "Historique refueling"
        CLOSING = "CLOSING", "Closing"

    class MovementType(models.TextChoices):
        DELIVERY = "DELIVERY", "Livraison confirmée"
        DELIVERY_EYDON = "DELIVERY_EYDON", "Livraison Eydon"
        DELIVERY_TOTAL_CARD = "DELIVERY_TOTAL_CARD", "Livraison carte Total"
        ADD = "ADD", "Appoint"
        TRANSFER_IN = "TRANSFER_IN", "Transfert entrant (ponction reçue)"
        TRANSFER_OUT = "TRANSFER_OUT", "Transfert sortant (ponction)"
        WITHDRAWAL = "WITHDRAWAL", "Retrait"
        THEFT = "THEFT", "Vol"
        DRAIN = "DRAIN", "Vidange"
        CORRECTION = "CORRECTION", "Correction"
        TRANSFERT_INTERSITE = "TRANSFERT_INTERSITE", "Transfert inter-sites (ancien libellé)"
        TRANSFERT_INTERSITE_SORTANT = "TRANSFERT_INTERSITE_SORTANT", "Transfert inter-sites sortant (Ponction, site source)"
        TRANSFERT_INTERSITE_ENTRANT = "TRANSFERT_INTERSITE_ENTRANT", "Transfert inter-sites entrant (Ponction, site destinataire)"

    class Status(models.TextChoices):
        DECLARED = "DECLARED", "Déclaré"
        CLOSED = "CLOSED", "Clôturé à la source"
        VALIDATED = "VALIDATED", "Validé"
        REJECTED = "REJECTED", "Rejeté"

    source_type = models.CharField(max_length=24, choices=SourceType.choices, db_index=True)
    source_event_id = models.CharField(max_length=128)
    batch = models.ForeignKey(FuelImportBatch, null=True, blank=True, on_delete=models.PROTECT, related_name="movements")
    import_row = models.ForeignKey(FuelImportRow, null=True, blank=True, on_delete=models.PROTECT, related_name="movements")
    site_id = models.CharField(max_length=64, db_index=True)
    counterpart_site_id = models.CharField(max_length=64, null=True, blank=True, help_text="Site / dépôt source ou destination d'un transfert.")
    tank_id = models.CharField(max_length=32, null=True, blank=True)
    movement_type = models.CharField(max_length=32, choices=MovementType.choices, null=True, blank=True, db_index=True)
    movement_datetime = models.DateTimeField(null=True, blank=True, db_index=True)
    requested_quantity_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    quantity_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Quantité réellement mouvementée ; NULL si non déclarée (jamais 0 par défaut).")
    level_before_raw = models.CharField(max_length=64, null=True, blank=True)
    level_after_raw = models.CharField(max_length=64, null=True, blank=True)
    hour_meter_before = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    hour_meter_after = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    supplier = models.CharField(max_length=128, null=True, blank=True)
    ticket_reference = models.CharField(max_length=128, null=True, blank=True)
    delivery_note = models.CharField(max_length=128, null=True, blank=True)
    proof_reference = models.CharField(max_length=512, null=True, blank=True)
    source_user = models.CharField(max_length=255, null=True, blank=True)
    source_status = models.CharField(max_length=64, null=True, blank=True, help_text="Statut brut à la source (ENOC pending/validated/done, fichier Soldé/En cours…).")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DECLARED, db_index=True)
    alerts = models.JSONField(default=list, blank=True, help_text="[{code, message}] — contrôles (sans ticket, sans preuve, demande seule…).")
    raw_operation_type = models.CharField(max_length=128, null=True, blank=True, help_text="Valeur brute de la source (type d'opération ENOC, source / sens du fichier).")
    source_category = models.CharField(max_length=32, null=True, blank=True, db_index=True, help_text="Classification (RELEVE_STOCK, LIVRAISON_CONFIRMEE, DEMANDE_LIVRAISON, …, INCONNU).")
    mapping_rule = models.CharField(max_length=255, null=True, blank=True, help_text="Règle de classification appliquée (version).")
    retained_in_stock = models.BooleanField(default=False, help_text="Mouvement réel exploitable pour le stock (jamais une demande ni une carte Total).")
    movement_direction = models.CharField(max_length=3, null=True, blank=True, help_text="IN / OUT (transferts inter-sites).")
    impact_stock_l = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True,
                                         help_text="Impact signé sur le stock du site (+ entrée, − sortie).")
    transfer_id = models.CharField(max_length=160, null=True, blank=True, db_index=True,
                                   help_text="Identifiant commun des deux écritures d'un transfert (somme des impacts = 0).")
    integration_status = models.CharField(max_length=28, null=True, blank=True, db_index=True,
                                          help_text="ENOC : INTEGRE / NON_INTEGRABLE / HISTORIQUE_NON_VERIFIABLE.")
    quality_status = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    quality_missing = models.JSONField(default=list, blank=True)
    date_source_used = models.CharField(max_length=16, null=True, blank=True,
                                        help_text="DATE_REELLE / DATE_VALIDATION / AUCUNE_DATE (provenance de movement_datetime).")
    source_provenance = models.CharField(max_length=32, null=True, blank=True, help_text="ENOC : provenance du statut brut.")
    business_status = models.CharField(max_length=28, null=True, blank=True, db_index=True, help_text="ENOC : statut métier eGrid.")
    status_mapping_rule = models.CharField(max_length=255, null=True, blank=True)
    eligible_for_stock = models.BooleanField(null=True, blank=True, help_text="ENOC : statut éligible au stock (NULL hors ENOC).")
    comment = models.TextField(null=True, blank=True)
    validated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    validated_at = models.DateTimeField(null=True, blank=True)
    validation_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_movement"
        ordering = ["-movement_datetime"]
        constraints = [models.UniqueConstraint(fields=["source_type", "source_event_id"], name="uniq_fuel_movement_source_event")]
        indexes = [models.Index(fields=["site_id", "movement_datetime"])]


class FuelRegistryCorrection(models.Model):
    """Piste d'audit : toute validation, rejet ou correction (ancienne valeur, nouvelle valeur, auteur, date, motif)."""

    entity = models.CharField(max_length=24, db_index=True, help_text="OBSERVATION, MOVEMENT, TANK, GUARD_SCOPE, BATCH.")
    entity_id = models.BigIntegerField(db_index=True)
    site_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    action = models.CharField(max_length=24, help_text="VALIDATE, REJECT, CORRECT, CREATE, SYSTEM.")
    field = models.CharField(max_length=64, null=True, blank=True)
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    reason = models.TextField()
    author = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    author_label = models.CharField(max_length=128, null=True, blank=True, help_text="« système » / « import #id » si pas d'utilisateur.")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "fuel_registry_correction"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["entity", "entity_id"])]


class FuelEnocTypeMapping(models.Model):
    """
    Classification administrable et versionnée des types d'opération ENOC. Une valeur brute non configurée
    est INCONNU (file d'exceptions) : jamais classée automatiquement comme mouvement réel.
    """

    class Category(models.TextChoices):
        RELEVE_STOCK = "RELEVE_STOCK", "Relevé de stock"
        LIVRAISON_CONFIRMEE = "LIVRAISON_CONFIRMEE", "Livraison confirmée"
        DEMANDE_LIVRAISON = "DEMANDE_LIVRAISON", "Demande de livraison (jamais une livraison)"
        AJOUT = "AJOUT", "Ajout"
        TRANSFERT_ENTRANT = "TRANSFERT_ENTRANT", "Transfert entrant"
        TRANSFERT_SORTANT = "TRANSFERT_SORTANT", "Transfert sortant"
        RETRAIT = "RETRAIT", "Retrait"
        VOL = "VOL", "Vol"
        VIDANGE = "VIDANGE", "Vidange"
        CARTE_TOTAL = "CARTE_TOTAL", "Carte Total (contrôle / preuve)"
        INCONNU = "INCONNU", "Inconnu"

    raw_value = models.CharField(max_length=128, db_index=True, help_text="Type d'opération ENOC normalisé (majuscules).")
    category = models.CharField(max_length=32, choices=Category.choices)
    version = models.IntegerField(default=1)
    active = models.BooleanField(default=True, db_index=True)
    comment = models.TextField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "fuel_enoc_type_mapping"
        ordering = ["raw_value", "-version"]


class FuelEnocQualifiedMovement(models.Model):
    """
    Contrat canonique d'un mouvement Fuel & GE ENOC dans eGrid + qualification métier + rapprochement.

    Trois couches séparées :
      - donnée brute : FuelEnocMovement.raw_payload (documents Mongo fuel_requests / fuel_operations, jamais modifiés) ;
      - extraction canonique (champs ci-dessous, field_sources = chemin brut de chaque valeur) ;
      - qualification eGrid (type brut ≠ impact stock) et rapprochement (seule voie vers ÉLIGIBLE_STOCK_RÉEL).
    Une valeur absente reste NULL.
    """

    class TypeFamily(models.TextChoices):
        TOTAL_CARD = "TOTAL_CARD", "Carte Total"
        PONCTION = "PONCTION", "Ponction"
        TRUCK = "TRUCK", "Truck"
        AUTRE = "AUTRE", "Autre type (conservé sans interprétation)"

    class ExecutionState(models.TextChoices):
        DEMANDE = "DEMANDE", "Demande"
        APPROUVEE = "APPROUVEE", "Approuvée"
        TERMINEE = "TERMINEE", "Terminée (incomplète)"
        QUANTITE_REELLE_MANQUANTE = "QUANTITE_REELLE_MANQUANTE", "Quantité réelle manquante"
        PREUVE_MANQUANTE = "PREUVE_MANQUANTE", "Preuve manquante"
        A_RAPPROCHER = "A_RAPPROCHER", "À rapprocher"
        ELIGIBLE_STOCK_REEL = "ELIGIBLE_STOCK_REEL", "Éligible stock réel"
        REJETEE = "REJETEE", "Rejetée"
        NATURE_A_QUALIFIER = "NATURE_A_QUALIFIER", "Nature à qualifier"
        TRANSFERT_A_COMPLETER = "TRANSFERT_A_COMPLETER", "Transfert à compléter"

    class TransferStatus(models.TextChoices):
        NON_APPLICABLE = "NON_APPLICABLE", "Non applicable (pas une Ponction)"
        TRANSFERT_A_COMPLETER = "TRANSFERT_A_COMPLETER", "Transfert à compléter"
        COMPTABILISE = "COMPTABILISE", "Comptabilisé (double écriture)"
        REJETE = "REJETE", "Rejeté"

    class FuelManagerAlert(models.TextChoices):
        AUCUNE = "AUCUNE", "Aucune"
        EN_ATTENTE = "EN_ATTENTE", "En attente Fuel Manager"
        TRAITEE = "TRAITEE", "Traitée par le Fuel Manager"

    class IntegrationStatus(models.TextChoices):
        INTEGRE = "INTEGRE", "Intégré au stock eGrid"
        NON_INTEGRABLE = "NON_INTEGRABLE", "Non intégrable (donnée minimale absente)"
        HISTORIQUE_NON_VERIFIABLE = "HISTORIQUE_NON_VERIFIABLE", "Historique non vérifiable"

    class DateSource(models.TextChoices):
        DATE_REELLE = "DATE_REELLE", "Date réelle d'opération"
        DATE_VALIDATION = "DATE_VALIDATION", "Date de validation"
        AUCUNE_DATE = "AUCUNE_DATE", "Aucune date"

    class Reconciliation(models.TextChoices):
        NON_REQUIS = "NON_REQUIS", "Non requis (aucun impact stock possible en l'état)"
        A_RAPPROCHER = "A_RAPPROCHER", "À rapprocher"
        RAPPROCHE = "RAPPROCHE", "Rapproché (stock / relevé terrain cohérent)"
        CERTIFIE = "CERTIFIE", "Certifié (rapproché + preuve fournisseur)"
        REJETE = "REJETE", "Rejeté au rapprochement"

    enoc_movement = models.OneToOneField(FuelEnocMovement, on_delete=models.PROTECT, related_name="qualified",
                                         help_text="raw_payload_reference : donnée brute ENOC conservée telle quelle.")
    enoc_request_id = models.CharField(max_length=128, null=True, blank=True)
    enoc_reference = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    site_id = models.CharField(max_length=128, null=True, blank=True, help_text="site_id brut ENOC")
    site_id_resolved = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    country = models.CharField(max_length=64, null=True, blank=True)
    zone = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    raw_movement_type = models.CharField(max_length=128, null=True, blank=True, db_index=True)
    raw_status = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    requested_quantity_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    approved_quantity_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    actual_quantity_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    quantity_unit = models.CharField(max_length=16, null=True, blank=True)
    planned_at = models.DateTimeField(null=True, blank=True, help_text="Date prévue de ravitaillement (demande).")
    actual_operation_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text="Date réelle (opération).")
    requested_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    tank_level_before_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    tank_level_after_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    tank_level_before_cm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    tank_level_after_cm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    generator_hourmeter_before_h = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    generator_hourmeter_after_h = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    technician_name = models.CharField(max_length=255, null=True, blank=True)
    validator_name = models.CharField(max_length=255, null=True, blank=True)
    closer_name = models.CharField(max_length=255, null=True, blank=True)
    comment = models.TextField(null=True, blank=True)
    proof_count = models.PositiveIntegerField(default=0)
    proof_urls_or_ids = models.JSONField(default=list, blank=True)
    supplier_name = models.CharField(max_length=255, null=True, blank=True)
    delivery_note_number = models.CharField(max_length=128, null=True, blank=True)
    vehicle_or_truck_reference = models.CharField(max_length=128, null=True, blank=True)
    operations_count = models.PositiveIntegerField(default=0, help_text="Nombre d'opérations ENOC liées à la demande.")
    field_sources = models.JSONField(default=dict, blank=True, help_text="Champ canonique → chemin brut ENOC utilisé.")
    source_fingerprint = models.CharField(max_length=64, blank=True, default="")
    synced_at = models.DateTimeField(null=True, blank=True)

    # Ponction (règle validée par Steve) : site_id principal = DESTINATAIRE, « Site source » = SOURCE ;
    # volume comptable = approved_quantity_l ; double écriture liée par transfer_id (somme des impacts = 0).
    transfer_id = models.CharField(max_length=160, null=True, blank=True, db_index=True)
    transfer_status = models.CharField(max_length=24, choices=TransferStatus.choices, default=TransferStatus.NON_APPLICABLE, db_index=True)
    ponction_source_site_id = models.CharField(max_length=128, null=True, blank=True, help_text="« Site source » brut ENOC")
    ponction_source_site_resolved = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    source_level_before_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    source_level_after_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    source_level_before_cm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    source_level_after_cm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    source_variation_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                             help_text="niveau après − niveau avant au site source (contrôle, jamais comptable)")
    control_gap_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                        help_text="abs(variation_source + quantité approuvée)")
    transfer_note = models.CharField(max_length=255, null=True, blank=True, help_text="Bon de transfert (référence ou pièce)")
    fuel_manager_alert = models.CharField(max_length=16, choices=FuelManagerAlert.choices, default=FuelManagerAlert.AUCUNE, db_index=True)
    fuel_manager_reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    fuel_manager_reviewed_at = models.DateTimeField(null=True, blank=True)
    fuel_manager_comment = models.TextField(null=True, blank=True)

    # Qualité NON exclusive : chaque indicateur est conservé indépendamment (un mouvement peut cumuler plusieurs défauts)
    source_record_id = models.CharField(max_length=128, null=True, blank=True, db_index=True, help_text="Identifiant source (FuelEnocMovement.source_id).")
    has_levels_before_after = models.BooleanField(default=False)
    has_proof = models.BooleanField(default=False)
    has_hourmeter = models.BooleanField(default=False)
    has_site_source = models.BooleanField(null=True, blank=True, help_text="Ponction seulement (NULL : sans objet).")
    has_site_destination = models.BooleanField(null=True, blank=True, help_text="Ponction seulement (NULL : sans objet).")
    has_approved_quantity = models.BooleanField(default=False)
    has_actual_quantity = models.BooleanField(default=False)
    has_date = models.BooleanField(default=False)
    level_coherence_status = models.CharField(max_length=24, null=True, blank=True, help_text="COHERENT / ECART / NON_CALCULABLE")
    stock_eligibility = models.CharField(max_length=24, null=True, blank=True, db_index=True, help_text="ELIGIBLE / NON_ELIGIBLE")
    quality_status_global = models.CharField(max_length=24, null=True, blank=True, db_index=True, help_text="COMPLET / DEFAUTS_MINEURS / DEFAUTS_MAJEURS / NON_INTEGRABLE")

    # Mapping figé du statut ENOC (enoc_canonical.map_status) : brut → statut métier eGrid, règle tracée
    source_provenance = models.CharField(max_length=32, null=True, blank=True, db_index=True,
                                         help_text="ENOC_MONGO / ENOC_OPERATION_SANS_DEMANDE / API_REST_HISTORIQUE")
    business_status = models.CharField(max_length=28, null=True, blank=True, db_index=True)
    status_mapping_rule = models.CharField(max_length=255, null=True, blank=True)
    eligible_for_stock = models.BooleanField(default=False, db_index=True)

    # Intégration eGrid sur données minimales : niveaux / photo / bon / compteur = qualité, jamais bloquants
    integration_status = models.CharField(max_length=28, choices=IntegrationStatus.choices, default=IntegrationStatus.NON_INTEGRABLE,
                                          db_index=True)
    non_integration_cause = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    quality_status = models.CharField(max_length=32, null=True, blank=True, db_index=True)
    quality_missing = models.JSONField(default=list, blank=True, help_text="Données de contrôle absentes (non bloquantes).")
    accounting_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                         help_text="Date comptable : date réelle, sinon date de validation, sinon NULL.")
    date_source_used = models.CharField(max_length=16, choices=DateSource.choices, default=DateSource.AUCUNE_DATE)
    integrated_quantity_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                                help_text="Volume intégré (Ponction : approuvé ; Truck / Carte Total : réel).")

    # Qualification eGrid (axe B — indépendant du type brut)
    type_family = models.CharField(max_length=16, choices=TypeFamily.choices, db_index=True)
    business_classification = models.CharField(max_length=48, db_index=True)
    execution_state = models.CharField(max_length=32, choices=ExecutionState.choices, db_index=True)
    stock_impact = models.BooleanField(default=False, db_index=True, help_text="Entre dans le stock réel (ÉLIGIBLE_STOCK_RÉEL seulement).")
    consumption_impact = models.BooleanField(default=False)
    reason = models.TextField(blank=True, default="")
    missing_conditions = models.JSONField(default=list, blank=True)
    alerts = models.JSONField(default=list, blank=True)
    blocking = models.BooleanField(default=False)
    qualification_version = models.CharField(max_length=32, default="enoc-q-v1")
    qualified_at = models.DateTimeField(null=True, blank=True)

    # Rapprochement (seule voie vers l'éligibilité au stock réel)
    reconciliation_status = models.CharField(max_length=16, choices=Reconciliation.choices, default=Reconciliation.NON_REQUIS,
                                             db_index=True)
    reconciled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciliation_reason = models.TextField(null=True, blank=True)
    reconciliation_evidence = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fuel_enoc_movement_qualified"
        ordering = ["-actual_operation_at", "-requested_at"]
        indexes = [models.Index(fields=["site_id_resolved", "actual_operation_at"])]


class FuelStockBalance(models.Model):
    """
    Bilan stock au grain site + période EXACTE (jamais limité au mois civil) :
        conso_reelle = stock_initial + livraisons_externes + carte_total + transferts_entrants + rajouts
                       − transferts_sortants − retraits − vols − vidanges − stock_final
    NULL reste NULL (stock manquant → conso NULL). Jamais « certifié » automatiquement : la certification est
    une action explicite du Fuel Manager (FuelStockBalanceCertification, historique non modifiable).
    """

    class Statut(models.TextChoices):
        PRET_A_CERTIFIER = "PRET_A_CERTIFIER", "Prêt à certifier"
        ESTIMATION_AVEC_RESERVE = "ESTIMATION_AVEC_RESERVE", "Estimation avec réserve"
        NON_CALCULABLE = "NON_CALCULABLE", "Non calculable"

    class Niveau(models.TextChoices):
        NON_CERTIFIE = "NON_CERTIFIE", "Non certifié"
        CERTIFIE = "CERTIFIE", "Certifié (Fuel Manager)"
        CERTIFIE_AVEC_RESERVE = "CERTIFIE_AVEC_RESERVE", "Certifié avec réserve (Fuel Manager)"
        A_RECERTIFIER = "A_RECERTIFIER", "À recertifier (données modifiées depuis la certification)"

    country = models.CharField(max_length=8, null=True, blank=True, db_index=True)
    site_id = models.CharField(max_length=64, db_index=True)
    site_name = models.CharField(max_length=255, null=True, blank=True)
    zone = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    typologie = models.CharField(max_length=255, null=True, blank=True, help_text="Typologie technique existante (installée).")
    profil_energetique = models.CharField(max_length=64, null=True, blank=True)
    observation_start = models.DateTimeField(db_index=True)
    observation_end = models.DateTimeField(db_index=True)

    stock_initial_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    source_stock_initial = models.CharField(max_length=32, null=True, blank=True)
    date_stock_initial = models.DateTimeField(null=True, blank=True)
    stock_initial_detail = models.JSONField(default=dict, blank=True)
    stock_final_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    source_stock_final = models.CharField(max_length=32, null=True, blank=True)
    date_stock_final = models.DateTimeField(null=True, blank=True)
    stock_final_detail = models.JSONField(default=dict, blank=True)

    truck_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    carte_total_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    transferts_entrants_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    transferts_sortants_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    vols_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    vidanges_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    retraits_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    rajouts_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    livraisons_externes_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                                help_text="Truck / Eydon et autres livraisons externes (hors Carte Total).")
    consommation_reelle_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    conso_journaliere_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                              help_text="Consommation réelle / jours entre les deux relevés (référence commande).")

    statut_bilan = models.CharField(max_length=32, choices=Statut.choices, db_index=True)
    niveau_certification = models.CharField(max_length=24, choices=Niveau.choices, default=Niveau.NON_CERTIFIE, db_index=True)
    motifs = models.JSONField(default=list, blank=True)
    alertes = models.JSONField(default=list, blank=True)
    resultat_bilan = models.CharField(max_length=32, null=True, blank=True, db_index=True,
                                      help_text="PRET_A_CERTIFIER / ESTIMATION_AVEC_RESERVE / NON_CALCULABLE")
    cause_principale = models.CharField(max_length=40, null=True, blank=True, db_index=True)
    score_confiance = models.IntegerField(null=True, blank=True)
    score_detail = models.JSONField(default=list, blank=True, help_text="Points gagnés / perdus par critère, avec motifs.")
    reserves = models.JSONField(default=list, blank=True)
    estimation = models.BooleanField(default=False, help_text="Au moins un stock estimé (cuve géométrique sans plaque / proposée).")
    age_stock_initial_j = models.IntegerField(null=True, blank=True)
    age_stock_final_j = models.IntegerField(null=True, blank=True)
    entrees_externes_nettes_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    volume_transfere_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    impact_ponctions_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True,
                                             help_text="Entrants − sortants au niveau du site (nul au niveau pays).")
    volume_brut_integre_l = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    proposition = models.JSONField(default=dict, blank=True, help_text="Données préparées pour la future recommandation de commande (indicatif).")
    candidats = models.JSONField(default=dict, blank=True, help_text="Relevés stock candidats (initial / final) et motif de sélection.")
    mouvements = models.JSONField(default=list, blank=True, help_text="Mouvements constitutifs retenus.")
    mouvements_exclus = models.JSONField(default=list, blank=True, help_text="Mouvements exclus ou à contrôler (avec motif).")
    periode_effective_debut = models.DateTimeField(null=True, blank=True)
    periode_effective_fin = models.DateTimeField(null=True, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True, default="")
    calculated_at = models.DateTimeField(default=timezone.now)
    rule_version = models.CharField(max_length=32, default="bilan-v1")

    class Meta:
        db_table = "fuel_stock_balance"
        constraints = [models.UniqueConstraint(fields=["site_id", "observation_start", "observation_end"], name="uniq_fuel_stock_balance_period")]
        indexes = [models.Index(fields=["observation_start", "observation_end", "statut_bilan"])]


class FuelStockBalanceCertification(models.Model):
    """Historique NON MODIFIABLE des certifications Fuel Manager (création seulement)."""

    class Action(models.TextChoices):
        CERTIFIE = "CERTIFIE", "Certifier"
        CERTIFIE_AVEC_RESERVE = "CERTIFIE_AVEC_RESERVE", "Certifier avec réserve"

    balance = models.ForeignKey(FuelStockBalance, on_delete=models.PROTECT, related_name="certifications")
    action = models.CharField(max_length=24, choices=Action.choices)
    comment = models.TextField(blank=True, default="")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+")
    user_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)
    fingerprint = models.CharField(max_length=64, help_text="Empreinte du bilan certifié (détecte une modification ultérieure).")
    snapshot = models.JSONField(default=dict, help_text="Valeurs du bilan au moment de la certification.")

    class Meta:
        db_table = "fuel_stock_balance_certification"
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValueError("Historique de certification non modifiable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Historique de certification non modifiable.")
