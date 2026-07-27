# certification/migrations/0001_initial.py
# À placer dans certification/migrations/0001_initial.py

from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("billing", "0001_initial"),   # adapte au nom de ta dernière migration billing
        ("core", "0001_initial"),       # adapte au nom de ta dernière migration core
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [

        # ── CertificationBatch ───────────────────────────────────────
        migrations.CreateModel(
            name="CertificationBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("echeance_year",  models.IntegerField(null=True, blank=True)),
                ("echeance_month", models.IntegerField(null=True, blank=True)),
                ("launched_at",  models.DateTimeField(auto_now_add=True)),
                ("finished_at",  models.DateTimeField(null=True, blank=True)),
                ("status",       models.CharField(max_length=16, choices=[("PENDING","En attente"),("RUNNING","En cours"),("DONE","Terminé"),("FAILED","Échoué")], default="PENDING", db_index=True)),
                ("celery_task_id", models.CharField(max_length=128, null=True, blank=True)),
                ("total",             models.IntegerField(default=0)),
                ("certified_fms",     models.IntegerField(default=0)),
                ("certified_senelec", models.IntegerField(default=0)),
                ("needs_review",      models.IntegerField(default=0)),
                ("unknown_contract",  models.IntegerField(default=0)),
                ("fms_unavailable",   models.IntegerField(default=0)),
                ("import_batch", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="certification_batch", to="billing.importbatch")),
                ("launched_by",  models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="certification_batches", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-launched_at"]},
        ),
        migrations.AddIndex(
            model_name="certificationbatch",
            index=models.Index(fields=["status"], name="certbatch_status_idx"),
        ),
        migrations.AddIndex(
            model_name="certificationbatch",
            index=models.Index(fields=["echeance_year", "echeance_month"], name="certbatch_echeance_idx"),
        ),

        # ── CertificationResult ──────────────────────────────────────
        migrations.CreateModel(
            name="CertificationResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("status", models.CharField(max_length=32, choices=[
                    ("PENDING_CERTIFICATION","En attente"),
                    ("UNKNOWN_CONTRACT","Contrat inconnu"),
                    ("FMS_UNAVAILABLE","FMS indisponible"),
                    ("CERTIFIED_FMS","Certifié FMS"),
                    ("CERTIFIED_SENELEC","Certifié Senelec"),
                    ("NEEDS_REVIEW","À analyser"),
                ], default="PENDING_CERTIFICATION", db_index=True)),
                ("computed_at", models.DateTimeField(auto_now=True)),

                # Étape 4
                ("conso_facturee_periode", models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("nb_jours_facturation",   models.IntegerField(null=True, blank=True)),
                ("conso_facturee_30j",     models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),

                # Étape 5
                ("fms_available",           models.BooleanField(default=False)),
                ("conso_fms_periode",       models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("conso_fms_30j",           models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("fms_last_complete_month", models.DateField(null=True, blank=True)),
                ("fms_error",               models.TextField(null=True, blank=True)),

                # Étape 6
                ("histo_last_conso", models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("histo_3mois_avg",  models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),

                # Étape 7
                ("ratio_fms_periode",  models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("ratio_fms_30j",      models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("ratio_histo_3mois",  models.DecimalField(max_digits=18, decimal_places=3, null=True, blank=True)),
                ("certified_by_rule",  models.CharField(max_length=16, choices=[("FMS_PERIODE","Ratio FMS / période"),("FMS_30J","Ratio FMS / 30 jours"),("HISTO_3MOIS","Historique 3 mois")], null=True, blank=True)),

                # FK
                ("cert_batch", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="results",  to="certification.certificationbatch")),
                ("invoice",    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="certifications", to="billing.sonatelinvoice")),
                ("site",       models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="certification_results", to="core.site")),
            ],
            options={"ordering": ["-computed_at"]},
        ),
        migrations.AddConstraint(
            model_name="certificationresult",
            constraint=models.UniqueConstraint(fields=["cert_batch", "invoice"], name="uniq_certresult_batch_invoice"),
        ),
        migrations.AddIndex(
            model_name="certificationresult",
            index=models.Index(fields=["cert_batch", "status"], name="certresult_batch_status_idx"),
        ),
        migrations.AddIndex(
            model_name="certificationresult",
            index=models.Index(fields=["site", "status"], name="certresult_site_status_idx"),
        ),
        migrations.AddIndex(
            model_name="certificationresult",
            index=models.Index(fields=["status"], name="certresult_status_idx"),
        ),

        # ── EfmsConnectionLog ────────────────────────────────────────
        migrations.CreateModel(
            name="EfmsConnectionLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ("attempted_at", models.DateTimeField(auto_now_add=True)),
                ("status",       models.CharField(max_length=16, choices=[
                    ("SUCCESS","Succès"),("TIMEOUT","Timeout"),
                    ("VPN_DOWN","VPN indisponible"),("AUTH_ERROR","Erreur auth"),
                    ("SQL_ERROR","Erreur SQL"),
                ], db_index=True)),
                ("duration_ms", models.IntegerField(null=True, blank=True)),
                ("host",        models.CharField(max_length=64, default="172.30.0.149")),
                ("query",       models.CharField(max_length=512, null=True, blank=True)),
                ("error",       models.TextField(null=True, blank=True)),
                ("cert_batch",  models.ForeignKey(null=True, blank=True, on_delete=django.db.models.deletion.SET_NULL, related_name="efms_logs", to="certification.certificationbatch")),
            ],
            options={"ordering": ["-attempted_at"]},
        ),
        migrations.AddIndex(
            model_name="efmsconnectionlog",
            index=models.Index(fields=["status", "attempted_at"], name="efmslog_status_at_idx"),
        ),
        migrations.AddIndex(
            model_name="efmsconnectionlog",
            index=models.Index(fields=["cert_batch"], name="efmslog_batch_idx"),
        ),
    ]