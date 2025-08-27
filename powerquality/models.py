from django.db import models
from energy.models import Country, Site


def dfield():  # Decimal générique robuste
    return models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)


class PQReport(models.Model):
    """
    Rapport de qualité/énergie par site et période (MonoPhase + TriPhase + TriPhase2).
    Unicité: (site, begin_period, end_period)
    """
    country       = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="pq_reports")
    site          = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="pq_reports")

    begin_period  = models.DateTimeField()
    end_period    = models.DateTimeField()
    extract_date  = models.DateTimeField(null=True, blank=True)

    # ---------------- MonoPhase ----------------
    mono_vmin_v   = dfield()
    mono_vavg_v   = dfield()
    mono_vmax_v   = dfield()
    mono_imin_a   = dfield()
    mono_iavg_a   = dfield()
    mono_imax_a   = dfield()
    mono_pmin_kw  = dfield()
    mono_pavg_kw  = dfield()
    mono_pmax_kw  = dfield()
    mono_total_energy_kwh     = dfield()
    mono_energy_consumed_kwh  = dfield()

    # ---------------- TriPhase (bloc principal) ----------------
    tri_vmin_u1_v = dfield(); tri_vavg_u1_v = dfield(); tri_vmax_u1_v = dfield()
    tri_vmin_u2_v = dfield(); tri_vavg_u2_v = dfield(); tri_vmax_u2_v = dfield()
    tri_vmin_u3_v = dfield(); tri_vavg_u3_v = dfield(); tri_vmax_u3_v = dfield()
    tri_imin_i1_a = dfield(); tri_iavg_i1_a = dfield(); tri_imax_i1_a = dfield()
    tri_imin_i2_a = dfield(); tri_iavg_i2_a = dfield(); tri_imax_i2_a = dfield()
    tri_imin_i3_a = dfield(); tri_iavg_i3_a = dfield(); tri_imax_i3_a = dfield()
    tri_pmin_kw   = dfield(); tri_pavg_kw   = dfield(); tri_pmax_kw   = dfield()
    tri_total_energy_kwh         = dfield()
    tri_active_energy_kwh        = dfield()
    tri_reactive_energy_kvarh    = dfield()
    tri_apparent_energy_kvah     = dfield()

    # ---------------- TriPhase 2 (second bloc) ----------------
    tri2_vmin_u1_v = dfield(); tri2_vavg_u1_v = dfield(); tri2_vmax_u1_v = dfield()
    tri2_vmin_u2_v = dfield(); tri2_vavg_u2_v = dfield(); tri2_vmax_u2_v = dfield()
    tri2_vmin_u3_v = dfield(); tri2_vavg_u3_v = dfield(); tri2_vmax_u3_v = dfield()
    tri2_imin_i1_a = dfield(); tri2_iavg_i1_a = dfield(); tri2_imax_i1_a = dfield()
    tri2_imin_i2_a = dfield(); tri2_iavg_i2_a = dfield(); tri2_imax_i2_a = dfield()
    tri2_imin_i3_a = dfield(); tri2_iavg_i3_a = dfield(); tri2_imax_i3_a = dfield()
    tri2_pmin_kw   = dfield(); tri2_pavg_kw   = dfield(); tri2_pmax_kw   = dfield()
    tri2_total_energy_kwh         = dfield()
    tri2_active_energy_kwh        = dfield()
    tri2_reactive_energy_kvarh    = dfield()
    tri2_apparent_energy_kvah     = dfield()

    # traces
    source_filename = models.CharField(max_length=255, blank=True, default="")
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("site", "begin_period", "end_period")
        indexes = [
            models.Index(fields=["begin_period"]),
            models.Index(fields=["end_period"]),
            models.Index(fields=["site", "begin_period"]),
            models.Index(fields=["country", "begin_period"]),
        ]
        ordering = ["-begin_period", "site__site_id"]

    def __str__(self):
        return f"{self.site.site_id} {self.begin_period:%Y-%m-%d}→{self.end_period:%Y-%m-%d}"
