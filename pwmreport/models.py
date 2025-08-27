# pwm/models.py
from django.db import models
from energy.models import Country, Site, InstallStatus  # réutilise vos modèles/choices

DEC = dict(max_digits=16, decimal_places=6, null=True, blank=True)

class PwmReport(models.Model):
    # période & méta
    country = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="pwm_reports")
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="pwm_reports")
    report_date = models.DateTimeField(null=True, blank=True)
    period_start = models.DateField()
    period_end = models.DateField()
    source_filename = models.CharField(max_length=255, null=True, blank=True)

    # infos site
    site_name = models.CharField(max_length=255, null=True, blank=True)
    site_class = models.CharField(max_length=64, null=True, blank=True)

    grid_status = models.CharField(max_length=8, choices=InstallStatus.choices, default=InstallStatus.NC)
    dg_status = models.CharField(max_length=8, choices=InstallStatus.choices, default=InstallStatus.NC)
    solar_status = models.CharField(max_length=8, choices=InstallStatus.choices, default=InstallStatus.NC)

    typology_power_w = models.IntegerField(null=True, blank=True)
    grid_act_pwm_avg_w = models.DecimalField(**DEC)

    # DC1..DC12 moyenne (W)
    dc1_pwm_avg_w  = models.DecimalField(**DEC);  dc2_pwm_avg_w  = models.DecimalField(**DEC)
    dc3_pwm_avg_w  = models.DecimalField(**DEC);  dc4_pwm_avg_w  = models.DecimalField(**DEC)
    dc5_pwm_avg_w  = models.DecimalField(**DEC);  dc6_pwm_avg_w  = models.DecimalField(**DEC)
    dc7_pwm_avg_w  = models.DecimalField(**DEC);  dc8_pwm_avg_w  = models.DecimalField(**DEC)
    dc9_pwm_avg_w  = models.DecimalField(**DEC);  dc10_pwm_avg_w = models.DecimalField(**DEC)
    dc11_pwm_avg_w = models.DecimalField(**DEC);  dc12_pwm_avg_w = models.DecimalField(**DEC)

    total_pwm_min_w  = models.DecimalField(**DEC)
    total_pwm_avg_w  = models.DecimalField(**DEC)
    total_pwm_max_w  = models.DecimalField(**DEC)
    total_pwc_avg_load_w = models.DecimalField(**DEC)

    dc_pwm_avg_uptime_pct = models.DecimalField(**DEC)
    pwc_uptime_pct        = models.DecimalField(**DEC)
    router_uptime_pct     = models.DecimalField(**DEC)

    typology_load_vs_pwm_real_load_pct = models.DecimalField(**DEC)
    grid_availability_pct  = models.DecimalField(**DEC)

    number_grid_cuts = models.IntegerField(null=True, blank=True)
    total_grid_cuts_minutes = models.IntegerField(null=True, blank=True)  # ex “HH:mm” => minutes

    class Meta:
        unique_together = ("site", "period_start", "period_end")
        indexes = [
            models.Index(fields=["period_start", "period_end"]),
            models.Index(fields=["country", "site"]),
        ]

    def __str__(self):
        return f"{self.site.site_id} [{self.period_start}..{self.period_end}]"
