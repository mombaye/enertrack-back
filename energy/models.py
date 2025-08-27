# energy/models.py

from django.db import models


# --- Réutilisable pour les statuts présents dans le fichier ---
class InstallStatus(models.TextChoices):
    YES = "YES", "Yes"
    NO = "NO", "No"
    NM = "NM", "Not Monitored / Monitorable"
    NI = "NI", "Not Installed"
    ODG = "0DG", "DG Not Installed"     # présent dans l’entête
    NC = "NC", "Not Collected / Not Calculated"  # pour valeurs non calculées


class Country(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name_plural = "countries"

    def __str__(self):
        return self.name


class EnergyMonthlyStat(models.Model):
    country = models.ForeignKey(Country, on_delete=models.CASCADE, related_name='energy_stats')
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()  # 1..12

    # Métriques
    sites_integrated = models.PositiveIntegerField(null=True, blank=True)
    sites_monitored  = models.PositiveIntegerField(null=True, blank=True)

    grid_mwh        = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    solar_mwh       = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    generators_mwh  = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    telecom_mwh     = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    grid_pct        = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    rer_pct         = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    generators_pct  = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)

    avg_telecom_load_mw = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)

    # Traces
    source_filename = models.CharField(max_length=255, blank=True, default='')
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('country', 'year', 'month')
        ordering = ['-year', '-month']

    def __str__(self):
        return f"{self.country} {self.year}-{self.month:02d}"


# --- Nouveaux modèles pour l’Energy Efficiency (par site & par mois) ---

class Site(models.Model):
    """
    Référentiel des sites (ID et nom tels que dans la feuille Excel)
    """
    country   = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="sites")
    site_id   = models.CharField(max_length=50, unique=True)   # ex: BKL_0086
    site_name = models.CharField(max_length=200)               # ex: BAKEL01

    class Meta:
        indexes = [
            models.Index(fields=["country", "site_id"]),
            models.Index(fields=["country", "site_name"]),
        ]

    def __str__(self) -> str:
        return f"{self.site_id} - {self.site_name}"


class SiteEnergyMonthlyStat(models.Model):
    """
    Photo mensuelle 'Energy Efficiency' par site.
    Aligne les colonnes :
      GRID / DG / Solar (statuts) ;
      GRID Energy [kWh], SOLAR Energy [kWh], TELECOM LOAD Energy [kWh] (kWh) ;
      GRID Energy [%], RER Renewable Energy Ratio [%] (ratios) ;
      Router/PwM/PwC Monitoring Availability [%] (disponibilités).
    """
    site  = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="monthly_stats")
    year  = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()  # 1..12

    # Statuts d’installation/monitoring (valeurs du fichier : YES/NO/NM/NI/0DG)
    grid_status  = models.CharField(max_length=4, choices=InstallStatus.choices, default=InstallStatus.YES)
    dg_status    = models.CharField(max_length=4, choices=InstallStatus.choices, default=InstallStatus.NO)
    solar_status = models.CharField(max_length=4, choices=InstallStatus.choices, default=InstallStatus.NO)

    # Energies kWh (certains champs peuvent être "NI"/"NC" dans le fichier -> on met NULL)
    grid_energy_kwh    = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True)
    solar_energy_kwh   = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True)
    telecom_load_kwh   = models.DecimalField(max_digits=12, decimal_places=0, null=True, blank=True)

    # Ratios (%) — on a vu des valeurs >100 (ex 126.6), donc on garde marge
    grid_energy_pct    = models.DecimalField(max_digits=6, decimal_places=1, null=True, blank=True)
    rer_pct            = models.DecimalField("RER Renewable Energy Ratio [%]", max_digits=6, decimal_places=1, null=True, blank=True)

    # Disponibilités monitoring (%)
    router_availability_pct = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)
    pwm_availability_pct    = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)
    pwc_availability_pct    = models.DecimalField(max_digits=12, decimal_places=1, null=True, blank=True)

    # Traces d’import
    source_filename = models.CharField(max_length=255, blank=True, default="")
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("site", "year", "month")
        ordering = ["site__site_id", "-year", "-month"]
        indexes = [
            models.Index(fields=["year", "month"]),
            models.Index(fields=["site", "year", "month"]),
        ]

    def __str__(self) -> str:
        return f"{self.site.site_id} {self.year}-{self.month:02d}"

    # Helpers pratiques pour l’import/affichage
    @property
    def has_numeric_grid(self) -> bool:
        return self.grid_energy_kwh is not None

    @property
    def has_numeric_solar(self) -> bool:
        return self.solar_energy_kwh is not None

    @property
    def has_numeric_telecom(self) -> bool:
        return self.telecom_load_kwh is not None
