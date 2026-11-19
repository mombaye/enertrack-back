from django.db import migrations, models


class Migration(migrations.Migration):
    """Faits journaliers Snowflake : niveau Fuel Sensor FMS et compteur horaire GE du jour (Suivi Conso sans appel Snowflake)."""

    dependencies = [
        ("fuel_tracking", "0058_niveau_snowflake_motif_ravit"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="fms_level_l",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True,
                                      help_text="LAST_VALID_LEVEL du jour (L), VW_FUEL_REPORT. Valeurs contradictoires → NULL."),
        ),
        migrations.AddField(
            model_name="fuelsitedailyfacts",
            name="dg_total_running_h",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=16, null=True,
                                      help_text="TOTAL_DG_RUNNING_HOUR du jour (h, compteur cumulé), GENSET_REPORT. Contradictoire → NULL."),
        ),
    ]
