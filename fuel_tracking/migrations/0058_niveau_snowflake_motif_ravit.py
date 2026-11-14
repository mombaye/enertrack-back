from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fuel_tracking", "0057_fuelstockbalance_conso_closing_l"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelconsommationmonthly",
            name="niveau_debut_snowflake_l",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Premier relevé TANK_LEVEL_AVG du mois (litres).",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="fuelconsommationmonthly",
            name="niveau_fin_snowflake_l",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Dernier relevé TANK_LEVEL_AVG du mois = stock effectif détecté par le capteur (litres).",
                max_digits=18,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="fuelconsommationmonthly",
            name="conso_estimee_sf_motif",
            field=models.CharField(
                blank=True,
                help_text="Motif quand conso_estimee_snowflake_l ne peut pas être calculée.",
                max_length=48,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="fuelconsommationmonthly",
            name="ravitaillement_non_enoc_sf_l",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Ravitaillement détecté par le capteur Snowflake non présent dans ENOC (litres).",
                max_digits=18,
                null=True,
            ),
        ),
    ]
