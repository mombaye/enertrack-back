# Generated manually 2026-09-23 — GENSET_REPORT fuel columns + FuelStockSnapshot monthly snapshots

from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ('fuel_tracking', '0034_fuelrapprochementthreshold_and_more'),
    ]

    operations = [
        # ── FuelCphGeDaily : colonnes brutes DSE (GENSET_REPORT) ─────────────
        migrations.AddField(
            model_name='fuelcphgedaily',
            name='controller_fuel_level_start',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=12, null=True,
                help_text='GENSET_REPORT.FUEL_LEVEL_START — niveau cuve début journée (brut DSE).',
            ),
        ),
        migrations.AddField(
            model_name='fuelcphgedaily',
            name='controller_fuel_level_end',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=12, null=True,
                help_text='GENSET_REPORT.FUEL_LEVEL_END — niveau cuve fin journée (brut DSE).',
            ),
        ),
        migrations.AddField(
            model_name='fuelcphgedaily',
            name='controller_fuel_consumed',
            field=models.DecimalField(
                blank=True, decimal_places=3, max_digits=12, null=True,
                help_text='GENSET_REPORT.FUEL_CONSUMED — consommation fuel journée (brut DSE).',
            ),
        ),

        # ── FuelStockSnapshot : supprimer la contrainte unique sur site_id ────
        # (remplacée par deux contraintes partielles ci-dessous)
        migrations.AlterField(
            model_name='fuelstocksnapshot',
            name='site_id',
            field=models.CharField(db_index=True, max_length=64),
        ),

        # ── FuelStockSnapshot : champs snapshot mensuel ───────────────────────
        migrations.AddField(
            model_name='fuelstocksnapshot',
            name='snapshot_year',
            field=models.IntegerField(
                blank=True, db_index=True, null=True,
                help_text='NULL = snapshot courant ; valeur = snapshot mensuel (année).',
            ),
        ),
        migrations.AddField(
            model_name='fuelstocksnapshot',
            name='snapshot_month',
            field=models.IntegerField(
                blank=True, null=True,
                help_text='NULL = snapshot courant ; valeur = snapshot mensuel (mois 1-12).',
            ),
        ),

        # ── FuelStockSnapshot : contraintes uniques partielles ────────────────
        migrations.AddConstraint(
            model_name='fuelstocksnapshot',
            constraint=models.UniqueConstraint(
                fields=['site_id'],
                condition=Q(snapshot_year__isnull=True),
                name='fuel_stock_snapshot_current_uniq',
            ),
        ),
        migrations.AddConstraint(
            model_name='fuelstocksnapshot',
            constraint=models.UniqueConstraint(
                fields=['site_id', 'snapshot_year', 'snapshot_month'],
                condition=Q(snapshot_year__isnull=False),
                name='fuel_stock_snapshot_monthly_uniq',
            ),
        ),
    ]
