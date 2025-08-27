# rectifiers/models.py
from django.db import models

# On réutilise le référentiel pays/sites de l'app energy
from energy.models import Country, Site


class RectifierReading(models.Model):
    """
    Une mesure (ex: avg_im_CurrentRectifierValue) par site et horodatage.
    """
    country      = models.ForeignKey(Country, on_delete=models.PROTECT, related_name="rectifier_readings")
    site         = models.ForeignKey(Site,     on_delete=models.CASCADE, related_name="rectifier_readings")

    param_name   = models.CharField(max_length=120)  # ex: 'avg_im_CurrentRectifierValue'
    param_value  = models.DecimalField(max_digits=16, decimal_places=6, null=True, blank=True)  # valeurs type 147.663194
    measure      = models.CharField(max_length=16, blank=True, default="")  # ex: 'A'
    measured_at  = models.DateTimeField()  # colonne "Date"

    # traces
    source_filename = models.CharField(max_length=255, blank=True, default="")
    imported_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("site", "param_name", "measured_at")
        indexes = [
            models.Index(fields=["measured_at"]),
            models.Index(fields=["param_name"]),
            models.Index(fields=["site", "measured_at"]),
            models.Index(fields=["country", "measured_at"]),
        ]
        ordering = ["-measured_at", "site__site_id"]

    def __str__(self) -> str:
        return f"{self.site.site_id} {self.param_name} @ {self.measured_at:%Y-%m-%d}"
