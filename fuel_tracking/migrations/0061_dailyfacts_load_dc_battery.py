from django.db import migrations, models


class Migration(migrations.Migration):
    """Faits journaliers Snowflake : load DC telco (AVERAGE_LOAD) et Ah batterie (BATTERY_REPORT) du jour — puissances
    médianes Load DC telco et recharge batterie du Suivi Conso, sans appel Snowflake à l'affichage."""

    dependencies = [
        ("fuel_tracking", "0060_base_ge_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="load_dc_w",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True,
                                      help_text="LOAD_W du jour (W, load DC telco moyen), AVERAGE_LOAD. Contradictoire → NULL."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="battery_charge_ah",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True,
                                      help_text="BATTERY_CHARGE_CURRENT_DAY_AH du jour (Ah), BATTERY_REPORT. Contradictoire → NULL."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="battery_discharge_ah",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True,
                                      help_text="BATTERY_DISCHARGE_CURRENT_DAY_AH du jour (Ah), BATTERY_REPORT. Contradictoire → NULL."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="battery_charge_w",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True,
                                      help_text="Médiane du jour de courant de charge × tension batterie (W), créneaux 5 min en "
                                                "charge, VW_GFMS_METRICS ; 0 si mesuré sans charge ; NULL si non mesuré."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="battery_charge_slots",
            field=models.IntegerField(blank=True, null=True, help_text="Créneaux 5 min en charge (courant > 0) du jour."),
        ),
    ]
