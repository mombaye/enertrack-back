from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('estimation', '0002_estimationbatch_count_senelec_and_more'),
        ('core', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ExternalEstimationUpload',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.IntegerField(verbose_name='Année')),
                ('month', models.IntegerField(verbose_name='Mois')),
                ('site_id_raw', models.CharField(db_index=True, max_length=50)),
                ('site_name_raw', models.CharField(blank=True, max_length=255)),
                ('conso_kwh', models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ('montant', models.DecimalField(blank=True, decimal_places=3, max_digits=16, null=True)),
                ('source_raw', models.CharField(blank=True, max_length=100)),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('site', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='core.site')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'Estimation externe',
                'verbose_name_plural': 'Estimations externes',
                'ordering': ['year', 'month', 'site__site_id'],
                'unique_together': {('year', 'month', 'site')},
            },
        ),
    ]
