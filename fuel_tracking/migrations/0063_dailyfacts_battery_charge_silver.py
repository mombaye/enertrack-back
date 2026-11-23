from django.db import migrations, models


class Migration(migrations.Migration):
    """Recharge batterie selon la règle de l'équipe data (SILVER.GFMS_METRICS, courant × tension par pas de 5 min)."""

    dependencies = [
        ("fuel_tracking", "0062_dailyfacts_rect_power"),
    ]

    operations = [
        migrations.RemoveField(model_name="fuelsitedailyfacts", name="battery_charge_w"),
        migrations.AlterField(
            model_name="fuelsitedailyfacts", name="battery_charge_slots",
            field=models.IntegerField(blank=True, null=True, help_text="Pas 5 min en charge (puissance > 0) du jour."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts", name="battery_telemetry_points",
            field=models.IntegerField(blank=True, null=True, help_text="Pas 5 min reçus du jour (tension batterie > 0, même courant nul)."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts", name="battery_charge_median_kw",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True,
                                      help_text="Médiane du jour des puissances de recharge > 0 (kW)."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts", name="battery_charge_median_current_a",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True,
                                      help_text="Courant de charge médian du jour sur les pas en charge (A)."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts", name="battery_charge_median_voltage_v",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=10, null=True,
                                      help_text="Tension batterie médiane du jour sur les pas en charge (V)."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts", name="battery_charge_values_kw",
            field=models.JSONField(blank=True, null=True,
                                   help_text="Puissances de recharge > 0 (kW) des jours NON qualifiés (< 50 % de 288) : repli période."),
        ),
    ]
