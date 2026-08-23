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
        colonnes prévues mais laissées vides tant que le format n'est pas défini) ;
      - pipeline CPH (fuel_tracking/services/fuel_cph_snowflake.py + commande
        sync_fuel_cph) : estimation par télémétrie GFMS_DATA_TRACKER_NC pour les
        sites sans capteur de cuve fiable — voir FuelCphGeDaily pour le détail
        journalier, ces colonnes ne sont que l'agrégat mensuel affiché en liste.

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

    # Estimation CPH (télémétrie GFMS_DATA_TRACKER_NC) — troisième source
    # d'estimation, indépendante des deux ci-dessus, pour les GE sans capteur
    # de cuve fiable. Agrégat mensuel calculé par sync_fuel_cph à partir du
    # détail journalier FuelCphGeDaily (voir ce modèle pour l'audit complet).
    # "Sans litre inventé" : conso_estimee_cph_l n'additionne QUE les jours au
    # statut OK ; cph_calculation_status/cph_status_breakdown expliquent
    # pourquoi les autres jours n'ont produit aucun litre.
    conso_estimee_cph_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)
    cph_l_per_h_moy = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True, help_text="Moyenne du CPH (L/h) sur les jours OK du mois.")
    cph_nb_jours_ok = models.IntegerField(null=True, blank=True, help_text="Nombre de jours du mois avec un statut OK (litres produits).")
    cph_nb_jours_calcules = models.IntegerField(null=True, blank=True, help_text="Nombre de jours du mois avec au moins un intervalle GE détecté (OK ou non).")
    cph_calculation_status = models.CharField(max_length=48, null=True, blank=True, help_text="Statut dominant du mois (OK, BATTERY_DATA_NOT_READY, MISSING_PARAMETER, ...).")
    cph_status_breakdown = models.JSONField(default=dict, blank=True, help_text='Répartition des statuts journaliers, ex. {"OK": 27, "BATTERY_DATA_NOT_READY": 2}.')
    cph_runtime_h_total = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Somme du runtime GE sur le mois — voir cph_runtime_source pour la source utilisée.")
    cph_runtime_source = models.CharField(
        max_length=24, null=True, blank=True,
        help_text=(
            "Origine de cph_runtime_h_total, priorité documentée dans la spec CPH "
            "(DSE > DG-On calculé) : TRACKER_5MIN (compteur GFMS_DATA_TRACKER_NC, "
            "seule source alimentant aussi le calcul des litres) ; DSE_CONTROLLER "
            "(GENSET_REPORT.DG_RUNTIME_CONTROLLER, utilisé en secours quand le "
            "compteur 5 min n'est jamais remonté par le site) ; DG_ON_CALCULATED "
            "(GENSET_REPORT.DG_RUNTIME_CALCULATED, dernier recours). Les 2 sources "
            "de secours donnent un Running Time mais PAS une estimation de litres "
            "(qui nécessite l'énergie du compteur 5 min, pas juste sa durée)."
        ),
    )
    cph_ge_type = models.CharField(max_length=160, null=True, blank=True, help_text="Marque + modèle du GE (Snowflake SITE_DG.VENDOR/GENSET_TYPE), auto-sourcé — voir fuel_cph_snowflake.fetch_site_ge_specs.")

    # Dernière valeur connue sur le mois — visibles sur la page Suivis
    # Consommation pour audit visuel (spec section 2.2/7), stable en
    # pratique (une fiche FuelCphGeParameter ne change pas d'un jour à
    # l'autre). Détail journalier complet dans FuelCphGeDaily.
    cph_pge_kva = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cph_power_factor = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    cph_spc_l_per_kwh = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)

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
    facturation_active_fichier = models.BooleanField(null=True, blank=True, help_text="Statut Facturation (Oui/Non) du fichier.")
    load_fichier_w = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True, help_text="Load (load reelle used), en W.")
    conso_fichier_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Genset fuel conso [L/Month] du fichier (Base GE.xlsx, colonne P) — total déclaratif mensuel, distinct de conso_estimee_fichier_l (calcul CPH interne au fichier).")
    ge_runtime_fichier_h = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Durée de fonctionnement du GE en h, valeur du mois telle que fournie par le fichier (Base GE.xlsx, colonne X) — pas de ÷12, ce fichier donne déjà un total mensuel (contrairement à l'ancien fichier annuel).")
    ge_prod_fichier_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Genset Production [kWh/y] du fichier ÷ 12.")
    fichier_source = models.CharField(max_length=160, null=True, blank=True, help_text="Nom/version du fichier d'origine, traçabilité.")

    # Champs supplémentaires Base GE.xlsx (2026-08, colonnes O/T/V/Y/AF) — le
    # fichier devient la source AFFICHÉE de ces mesures pour Suivis
    # Consommation (au lieu du pipeline CPH Snowflake, trop partiel : 201/483
    # sites seulement). pge_kva_fichier est dupliqué depuis
    # FuelCphGeParameter.pge_kva (déjà alimenté par import_base_ge) pour
    # rester disponible même sur les sites où sync_fuel_cph n'a produit
    # aucun jour OK — jamais fusionné avec cph_pge_kva (Snowflake SITE_DG).
    pge_kva_fichier = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Puissance GE (KVA), Base GE.xlsx colonne O.")
    ge_load_pct_fichier = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, help_text="GE load percentage en %, Base GE.xlsx colonne T.")
    cph_lph_fichier = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True, help_text="Cph en L/h, Base GE.xlsx colonne V.")
    conso_estimee_fichier_l = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Consommation de carburant en L, Base GE.xlsx colonne Y — renseigné pour seulement 5 des 469 lignes (valeurs 2,3,4,5,6h en colonne Running Time, motif manifestement factice/exemple) : conservé pour audit, mais PLUS utilisé pour la colonne affichée Conso estimée (voir conso_estimee_aout26_l et conso_estimee_cph_l).")
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

    # Détail énergie CPH (télémétrie GFMS_DATA_TRACKER_NC) — agrégat mensuel
    # (SUM des jours OK/OVER_CAPACITY uniquement, même garde que
    # conso_estimee_cph_l) du détail journalier FuelCphGeDaily. Absent du
    # fichier Base GE.xlsx (aucune colonne équivalente) : seule source
    # possible pour ces 4 métriques, voir fuel_cph_service.py.
    cph_site_load_energy_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Énergie site (charge) cumulée sur les jours OK du mois, kWh.")
    cph_battery_dc_energy_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Énergie de recharge batterie côté DC cumulée sur les jours OK du mois, kWh.")
    cph_battery_ac_energy_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Énergie de recharge batterie côté AC (DC ÷ rendement redresseur) cumulée sur les jours OK du mois, kWh.")
    cph_total_ge_energy_kwh = models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True, help_text="Énergie totale fournie par le GE (site + recharge batterie AC) cumulée sur les jours OK du mois, kWh.")

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


class FuelCphGeParameter(models.Model):
    """
    Fichier de référence GE — paramètres fixes nécessaires au calcul CPH
    (fuel_tracking/services/fuel_cph_snowflake.py), un par site et par
    période de validité (une seule fiche active à une date donnée).

    Chargé via la commande import_cph_ge_parameters (upsert par
    site_id+valid_from — jamais un remplacement complet : contrairement à
    CphMatrixPoint/FuelSiteScope, ce modèle est temporel et l'historique
    clôturé (valid_to renseigné) doit être conservé pour pouvoir ré-exécuter
    ou auditer des mois passés).

    valid_to = None signifie "toujours active depuis valid_from".

    pge_kva/power_factor NE SONT PAS demandés au fichier — ils sont
    auto-sourcés depuis Snowflake SITE_DG (KVA réel par site, vérifié sur les
    10 sites pilotes) et n'affectent de toute façon PAS le calcul des litres
    (seulement l'indicateur informatif GE_LOAD_PERCENT/OVER_CAPACITY) — voir
    fuel_tracking/services/fuel_cph_snowflake.py::fetch_site_ge_specs. Ces 2
    champs restent ici en secours/override manuel uniquement (ex: SITE_DG
    absent ou erroné pour un site donné), jamais requis à l'import.

    rectifier_efficiency_ratio, comme pge_kva/power_factor, est maintenant
    OPTIONNEL (2026-08) : auto-sourcé depuis la moyenne mensuelle Snowflake
    GFMS_DATA_TRACKER_NC.RECTIFIER_EFFICIENCY si absent du fichier — voir
    fuel_cph_snowflake.fetch_site_rectifier_efficiency. Couverture vérifiée
    ~46% des sites Sénégal avec GE, valeurs plausibles (médiane ~70%). Reste
    ici en override manuel si besoin (ex: télémétrie absente/douteuse pour un
    site donné).

    spc_l_per_kwh, en revanche, reste le SEUL champ vraiment requis : il
    entre directement dans le calcul des litres et AUCUNE source Snowflake
    fiable n'a été trouvée pour le dériver automatiquement (vérifié 2026-08,
    2 méthodes indépendantes : GE_PROD_KWH, censé permettre un SPC empirique
    via conso_specifique_moy_l_kwh, reste trop peu fiable même en ne gardant
    que les sites à production GE substantielle — valeurs de 0.58 à 5.69
    L/kWh sur les 8 seuls points disponibles, contre un ordre de grandeur
    réaliste de 0.2-0.4 L/kWh ; aucune table de référence déjà en base
    — CphMatrixPoint, GensetFuelCurve — n'est peuplée).
    """

    site_id = models.CharField(max_length=64, db_index=True)
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    pge_kva = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True, help_text="Puissance nominale du GE, en kVA. Optionnel — auto-sourcé depuis Snowflake SITE_DG si absent.")
    power_factor = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True, help_text="Cos φ, strictement entre 0 et 1. Optionnel — 0.8 (valeur standard) utilisé si absent.")
    rectifier_efficiency_ratio = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True, help_text="Rendement du redresseur, strictement entre 0 et 1. Optionnel — moyenne mensuelle Snowflake (RECTIFIER_EFFICIENCY) utilisée si absent.")
    spc_l_per_kwh = models.DecimalField(max_digits=8, decimal_places=4, help_text="Consommation spécifique du GE, en L/kWh. SEUL champ requis — entre dans le calcul des litres, aucune source Snowflake fiable.")

    ge_type = models.CharField(max_length=128, blank=True, help_text="Optionnel — auto-sourcé depuis Snowflake SITE_DG.GENSET_TYPE si absent.")
    parameter_source = models.CharField(max_length=128, blank=True, help_text="Traçabilité : nom/version du fichier métier d'origine.")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Paramètres GE (CPH)"
        verbose_name_plural = "Paramètres GE (CPH)"
        ordering = ["site_id", "valid_from"]
        constraints = [
            models.UniqueConstraint(fields=["site_id", "valid_from"], name="uniq_cph_param_site_validfrom"),
        ]
        indexes = [
            models.Index(fields=["site_id", "valid_to"]),
        ]

    def __str__(self):
        return f"{self.site_id} · {self.valid_from} → {self.valid_to or '…'}"


class FuelCphGeDaily(models.Model):
    """
    Détail journalier du calcul CPH par site — source de vérité et piste
    d'audit derrière les agrégats mensuels de FuelConsommationMonthly
    (conso_estimee_cph_l et champs associés). Une ligne par (site_id, date),
    que le statut soit OK ou non : c'est ce qui permet de comprendre "pourquoi
    le chiffre du site X est bas ce mois-ci" sans re-requêter Snowflake.

    Reproduit EXACTEMENT le schéma de sortie documenté par la spec (section
    2.2 du deployment spec / "Sortie attendue" de l'IT handoff) : COUNTRY,
    SITE_ID, DATE, DATA_ID, DG_RUNTIME_BUSINESS_H(+SOURCE), DG_RUNTIME_INTERVAL_H
    (+STATUS), les énergies, PGE_KVA/POWER_FACTOR/GE_LOAD_PERCENT, SPC_L_PER_KWH,
    CPH_ESTIMATED_LPH, ESTIMATED_CONSUMPTION_L, CALCULATION_STATUS.

    Deux runtimes distincts, jamais confondus (spec section 6, "ne pas
    mélanger les deux besoins") :
      - dg_runtime_controller_h (DSE seul) : utilisé UNIQUEMENT pour valider
        que dg_runtime_interval_h (compteur tracker) est fiable ce jour-là
        (tolérance 0.15h) — conditionne le calcul des litres.
      - dg_runtime_business_h/source (DSE > DG-On calculé > redresseur 5 min
        pour les hybrides solaire+GE sans DSE) : le runtime "métier" à
        afficher, pas utilisé pour valider les litres.
    Les deux compteurs GENSET_REPORT sont bornés à [0, 24]h à la source
    (Snowflake) : des valeurs aberrantes jusqu'à ~1,2 million d'heures pour
    une seule journée ont été constatées (2026-08, compteur cumulatif mal
    réinitialisé) — nullifiées avant d'atteindre cette table.

    pge_kva/power_factor/spc_l_per_kwh sont les valeurs RÉELLEMENT utilisées
    ce jour-là (fichier de référence ou repli Snowflake), stockées ici pour
    audit complet — distinctes de FuelCphGeParameter qui ne garde que la
    définition de référence, pas la valeur appliquée par jour.

    "Sans litre inventé" : estimated_consumption_l et cph_estimated_lph ne
    sont renseignés que si calculation_status == "OK" (ou "OVER_CAPACITY").
    """

    class Status(models.TextChoices):
        OK = "OK", "OK"
        NO_VALID_RUNTIME = "NO_VALID_RUNTIME", "Aucun runtime métier valide"
        RUNTIME_NOT_VALIDATED = "RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH", "Runtime non validé pour le CPH"
        MISSING_LOAD_POWER = "MISSING_LOAD_POWER", "LOAD_POWER indisponible"
        BATTERY_DATA_NOT_READY = "BATTERY_DATA_NOT_READY", "Données batterie insuffisantes"
        MISSING_PARAMETER = "MISSING_PARAMETER", "Paramètres GE manquants"
        OVER_CAPACITY = "OVER_CAPACITY", "Charge GE > capacité déclarée"

    country = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    site_id = models.CharField(max_length=64, db_index=True)
    date = models.DateField(db_index=True)
    data_id = models.IntegerField(null=True, blank=True, help_text="Traçabilité Snowflake (SITE_FILTERED.DATA_ID) — pas la clé métier, voir contrainte (site_id, date).")

    # Détection d'intervalles (GFMS_DATA_TRACKER_NC, run_minutes 1-10 et
    # elapsed_minutes 1-10) + comparaison au runtime DSE (GENSET_REPORT.
    # DG_RUNTIME_CONTROLLER), tolérance 0.15h.
    ge_intervals = models.IntegerField(default=0)
    valid_battery_intervals = models.IntegerField(default=0)
    dg_runtime_interval_h = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dg_runtime_interval_status = models.CharField(max_length=24, null=True, blank=True, help_text="VALID si dg_runtime_interval_h > 0 (énergie intégrable ce jour), NO_INTERVALS sinon.")
    dg_runtime_controller_h = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True, help_text="DSE (GENSET_REPORT.DG_RUNTIME_CONTROLLER) — sert UNIQUEMENT à valider dg_runtime_interval_h, jamais au runtime affiché.")

    # Runtime "métier" à 3 sources (DSE > DG-On calculé > redresseur 5 min) —
    # colonne d'AFFICHAGE, distincte de dg_runtime_controller_h ci-dessus.
    dg_runtime_business_h = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dg_runtime_business_source = models.CharField(max_length=24, null=True, blank=True, help_text="DSE_CONTROLLER, DG_ON_CALCULATED, RECTIFIER_STATUS_5MIN ou NO_VALID_RUNTIME.")

    # Énergies (kWh), intégrées uniquement pendant les intervalles GE actifs
    site_load_energy_kwh = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    battery_dc_energy_kwh = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    battery_charge_ac_energy_kwh = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    total_ge_energy_kwh = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    average_ge_power_kw = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)

    # Paramètres GE réellement appliqués ce jour (fichier ou repli Snowflake)
    pge_kva = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    power_factor = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    spc_l_per_kwh = models.DecimalField(max_digits=8, decimal_places=4, null=True, blank=True)

    # Résultat carburant (uniquement si calculation_status == OK)
    cph_estimated_lph = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    estimated_consumption_l = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    ge_load_percent = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True, help_text="100 × average_ge_power_kw / (PGE_KVA × power_factor). Informatif, ne divise pas le CPH.")

    calculation_status = models.CharField(max_length=48, choices=Status.choices, db_index=True)

    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Détail CPH journalier"
        verbose_name_plural = "Détails CPH journaliers"
        ordering = ["-date", "site_id"]
        constraints = [
            models.UniqueConstraint(fields=["site_id", "date"], name="uniq_cph_daily_site_date"),
        ]
        indexes = [
            models.Index(fields=["site_id", "date"]),
        ]

    def __str__(self):
        return f"{self.site_id} · {self.date} · {self.calculation_status}"


class FuelCphSyncRun(models.Model):
    """Traçabilité des exécutions de sync_fuel_cph (même principe que FuelConsommationSyncRun)."""

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
        return f"Fuel CPH sync {self.month_from}→{self.month_to} [{self.status}]"