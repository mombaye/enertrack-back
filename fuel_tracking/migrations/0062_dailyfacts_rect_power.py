from django.db import migrations, models


class Migration(migrations.Migration):
    """Faits journaliers : puissance redresseur selon la règle de l'équipe data (P_DC ÷ rendement, médiane, disponibilité)."""

    dependencies = [
        ("fuel_tracking", "0061_dailyfacts_load_dc_battery"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="rect_power_points",
            field=models.IntegerField(blank=True, null=True,
                                      help_text="Mesures redresseur valides du jour (Good/Aged/Warning, P_DC > 0, rendement 70-100 %)."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="rect_power_median_kw",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True,
                                      help_text="Médiane du jour de P_DC ÷ (rendement/100) ÷ 1000 (kW), mesures valides."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="rect_power_values_kw",
            field=models.JSONField(blank=True, null=True,
                                   help_text="Mesures valides (kW) des jours NON qualifiés (< 50 % de 288) : repli médiane période."),
        ),
    ]
