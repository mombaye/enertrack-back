from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fuel_tracking", "0035_fuelcphgedaily_controller_fuel_fields_fuelstocksnapshot_monthly"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelrapprochementthreshold",
            name="seuil_ok_l",
            field=models.DecimalField(
                decimal_places=2, default=100, max_digits=10,
                help_text="Plancher absolu (L) pour le seuil OK : max(ce seuil, seuil_ok_pct% × conso_ref).",
            ),
        ),
        migrations.AddField(
            model_name="fuelrapprochementthreshold",
            name="seuil_aj_l",
            field=models.DecimalField(
                decimal_places=2, default=200, max_digits=10,
                help_text="Plancher absolu (L) pour le seuil A_JUSTIFIER : max(ce seuil, seuil_a_justifier_pct% × conso_ref).",
            ),
        ),
        migrations.AlterField(
            model_name="fuelrapprochementthreshold",
            name="seuil_ok_pct",
            field=models.DecimalField(
                decimal_places=2, default=10, max_digits=5,
                help_text="Écart ≤ max(seuil_ok_l, seuil_ok_pct% × conso_ref) → statut OK.",
            ),
        ),
        migrations.AlterField(
            model_name="fuelrapprochementthreshold",
            name="seuil_a_justifier_pct",
            field=models.DecimalField(
                decimal_places=2, default=20, max_digits=5,
                help_text="Écart ≤ max(seuil_aj_l, seuil_a_justifier_pct% × conso_ref) → A_JUSTIFIER ; au-delà → A_INVESTIGUER.",
            ),
        ),
    ]
