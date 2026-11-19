from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("fuel_tracking", "0057_fuelstockbalance_conso_closing_l")]

    operations = [
        migrations.AlterField(
            model_name="fuelimportbatch",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("ENOC", "ENOC"),
                    ("LOLIFORCE", "Loliforce — relevés de stock terrain"),
                    ("SCSS", "SCSS — relevés de stock terrain"),
                    ("CLOSING", "Closing mensuel — stocks et mouvements de période"),
                    ("HISTORICAL_REFUELING", "Historique refueling — source historique"),
                    ("TANK_MODELS", "Modèles de cuves — calculateur et conversion cm vers litres"),
                    ("TARGET_FUEL", "Target fuel mensuel par site"),
                    ("TERRAIN_MENSUEL", "Remontées terrain mensuelles — fichier complémentaire"),
                    ("BASE_GE", "Base GE — type de GE par site"),
                ], db_index=True, max_length=32
            ),
        ),
    ]
