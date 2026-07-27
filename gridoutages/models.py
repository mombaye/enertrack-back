from django.db import models


class GridOutageDaily(models.Model):
    """
    File 1 : données agrégées par jour
    Country | Site ID | Param Name | Param Value | Measure | Date
    """

    country = models.CharField(max_length=64)
    site_id = models.CharField(max_length=32, db_index=True)
    param_name = models.CharField(max_length=64)  # ex: "day_grid_outage"
    param_value = models.DecimalField(max_digits=10, decimal_places=2)
    measure = models.CharField(max_length=16)     # ex: "Min"
    date = models.DateTimeField()

    class Meta:
        db_table = "grid_outage_daily"
        unique_together = ("site_id", "param_name", "date")
        indexes = [
            models.Index(fields=["site_id", "date"]),
        ]

    def __str__(self):
        return f"{self.site_id} {self.param_name} {self.date.date()}"


class GridOutageAlarm(models.Model):
    """
    File 2 : alarmes FMS
    ID | Client | Site ID | Alarm Name | Alarm ID | ... | Date Start | Date End | Ticket ID
    """

    # ID du fichier, unique => clé primaire
    id = models.BigIntegerField(primary_key=True)
    client = models.CharField(max_length=128)
    site_id = models.CharField(max_length=32, db_index=True)

    alarm_name = models.CharField(max_length=128)
    alarm_code = models.CharField(max_length=32, blank=True)  # "Alarm ID"
    alarm_details = models.TextField(blank=True)

    equip_ip = models.CharField(max_length=64, blank=True)
    equip_name = models.CharField(max_length=128, blank=True)

    alarm_severity = models.CharField(max_length=32)
    status = models.CharField(max_length=32)
    username = models.CharField(max_length=64, blank=True)

    date_start = models.DateTimeField()
    date_end = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=255, blank=True)
    ticket_id = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "grid_outage_alarm"
        indexes = [
            models.Index(fields=["site_id", "date_start"]),
            models.Index(fields=["client"]),
        ]

    def __str__(self):
        return f"{self.id} - {self.site_id} - {self.date_start}"
