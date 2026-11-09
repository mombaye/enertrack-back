"""
Migration 0057 — Ajout de conso_closing_l et source_conso_closing sur FuelStockBalance.

conso_closing_l : valeur extraite du fichier Closing mensuel (onglet "NR analyse variation
conso fuel"). Champ first-class pour remplacer la lecture JSON imbriquée dans
stock_initial_detail / stock_final_detail.

source_conso_closing : provenance (toujours "CLOSING" pour l'instant).

Réversible : les deux champs sont nullable — le retour arrière supprime simplement
les colonnes, sans perte de données critiques (les valeurs restent dans les JSON existants).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fuel_tracking", "0056_terrain_mensuel_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="fuelstockbalance",
            name="conso_closing_l",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text=(
                    "Consommation Closing : valeur extraite du fichier Closing mensuel de l'opérateur "
                    "(onglet 'NR analyse variation conso fuel'). NULL si aucun Closing importé pour la période. "
                    "Ne jamais substituer à variation_stock_l — ce sont deux mesures indépendantes."
                ),
                max_digits=14,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="fuelstockbalance",
            name="source_conso_closing",
            field=models.CharField(
                blank=True,
                help_text="Source de conso_closing_l : 'CLOSING' si importé via FuelImportBatch.",
                max_length=32,
                null=True,
            ),
        ),
    ]
