# Generated manually 2026-09-21 — spec C : rapprochement stock + disponibilité runtime CPH

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('fuel_tracking', '0033_fuelcphgedaily_conso_estimee_source_and_more'),
    ]

    operations = [
        # Nouveau modèle FuelRapprochementThreshold
        migrations.CreateModel(
            name='FuelRapprochementThreshold',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(default='default', help_text="Identifiant du jeu de seuils ('default' = jeu actif).", max_length=64, unique=True)),
                ('seuil_ok_pct', models.DecimalField(decimal_places=2, default=10, help_text='Écart absolu ≤ ce seuil → statut OK.', max_digits=5)),
                ('seuil_a_justifier_pct', models.DecimalField(decimal_places=2, default=20, help_text='seuil_ok_pct < écart absolu ≤ ce seuil → A_JUSTIFIER ; au-delà → A_INVESTIGUER.', max_digits=5)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Seuil de rapprochement carburant',
                'verbose_name_plural': 'Seuils de rapprochement carburant',
            },
        ),
        # Disponibilité runtime CPH
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='cph_runtime_availability_pct',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='% jours du mois avec runtime GE valide (toutes sources confondues) — règle disponibilité ≥ 50 %.', max_digits=5, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='cph_runtime_source_availability',
            field=models.JSONField(blank=True, default=None, help_text='% jours du mois avec runtime valide par source, ex. {"DSE_CONTROLLER": 87.5, "TRACKER_5MIN": 12.5}.', null=True),
        ),
        # Rapprochement stock
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_stock_initial_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Stock initial du mois (FuelStockSnapshot.stock_snowflake_l ou fichier gardien).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_livraisons_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Livraisons du mois (enoc_qte_ajoutee_l ; flagué LIVRAISONS_ENOC_A_CONTROLER si = 0 L).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_rajouts_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Rajouts manuels du mois (fichier observation).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_retraits_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Retraits autorisés du mois (fichier observation).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_vols_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Vols déclarés du mois (fichier observation).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_vidanges_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Vidanges du mois (fichier observation).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_stock_final_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Stock final du mois (FuelStockSnapshot.stock_snowflake_l ou fichier gardien).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_conso_stock_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Conso balance stock : initial + livraisons + rajouts − retraits − vols − vidanges − final.', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_ecart_l',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Écart conso mesurée/estimée vs conso stock (L).', max_digits=18, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_ecart_pct',
            field=models.DecimalField(blank=True, decimal_places=2, help_text='Écart en % de la conso de référence.', max_digits=7, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_statut',
            field=models.CharField(blank=True, help_text='Statut rapprochement : OK / A_JUSTIFIER / A_INVESTIGUER / DONNEES_INCOMPLETES / CPH_NON_CALCULE.', max_length=24, null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='rapprochement_motif',
            field=models.TextField(blank=True, help_text='Motif lisible du statut rapprochement.', null=True),
        ),
        migrations.AddField(
            model_name='fuelconsommationmonthly',
            name='livraisons_source',
            field=models.CharField(blank=True, help_text='Source livraisons : ENOC_REEL ou LIVRAISONS_ENOC_A_CONTROLER (données à contrôler).', max_length=48, null=True),
        ),
    ]
