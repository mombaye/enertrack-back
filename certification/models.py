# certification/models.py
# PATCH v4 — Grid primaire, ACM fallback
#   + statut MESURE_A_VERIFIER (alerte 50%)
#   + flag_mesure_alert sur CertificationResult
#   + compteur mesure_alert sur CertificationBatch

from django.db import models
from django.conf import settings
from billing.models import ImportBatch, SonatelInvoice

DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class CertificationBatch(models.Model):

    class Status(models.TextChoices):
        PENDING = "PENDING", "En attente"
        RUNNING = "RUNNING", "En cours"
        DONE    = "DONE",    "Terminé"
        FAILED  = "FAILED",  "Échoué"

    import_batch = models.OneToOneField(
        ImportBatch,
        on_delete=models.CASCADE,
        related_name="certification_batch",
    )
    echeance_year  = models.IntegerField(null=True, blank=True)
    echeance_month = models.IntegerField(null=True, blank=True)
    launched_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="certification_batches",
    )
    launched_at    = models.DateTimeField(auto_now_add=True)
    finished_at    = models.DateTimeField(null=True, blank=True)
    status         = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    celery_task_id = models.CharField(max_length=128, null=True, blank=True)

    total             = models.IntegerField(default=0)
    certified_fms     = models.IntegerField(default=0)
    certified_senelec = models.IntegerField(default=0)
    needs_review      = models.IntegerField(default=0)
    unknown_contract  = models.IntegerField(default=0)
    fms_unavailable   = models.IntegerField(default=0)
    # ✅ v4 — compteur alerte mesure
    mesure_alert      = models.IntegerField(default=0)

    class Meta:
        ordering = ["-launched_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["echeance_year", "echeance_month"]),
        ]

    def __str__(self):
        return (
            f"CertBatch #{self.id} | "
            f"{self.echeance_year}-{str(self.echeance_month or '').zfill(2)} | "
            f"{self.status}"
        )

    @property
    def echeance_label(self) -> str:
        if self.echeance_year and self.echeance_month:
            return f"{self.echeance_year}-{str(self.echeance_month).zfill(2)}"
        return "N/A"

    def refresh_counters(self):
        from django.db.models import Count
        agg = self.results.values("status").annotate(n=Count("id"))
        mapping = {r["status"]: r["n"] for r in agg}
        self.total             = self.results.count()
        self.certified_fms     = mapping.get(CertificationResult.Status.CERTIFIED_FMS, 0)
        self.certified_senelec = mapping.get(CertificationResult.Status.CERTIFIED_SENELEC, 0)
        self.needs_review      = mapping.get(CertificationResult.Status.NEEDS_REVIEW, 0)
        self.unknown_contract  = mapping.get(CertificationResult.Status.UNKNOWN_CONTRACT, 0)
        self.fms_unavailable   = mapping.get(CertificationResult.Status.FMS_UNAVAILABLE, 0)
        # ✅ v4
        self.mesure_alert      = mapping.get(CertificationResult.Status.MESURE_A_VERIFIER, 0)
        self.save(update_fields=[
            "total", "certified_fms", "certified_senelec",
            "needs_review", "unknown_contract", "fms_unavailable",
            "mesure_alert",  # ✅ v4
        ])


class CertificationResult(models.Model):

    class Status(models.TextChoices):
        PENDING_CERTIFICATION = "PENDING_CERTIFICATION", "En attente"
        UNKNOWN_CONTRACT      = "UNKNOWN_CONTRACT",      "Contrat inconnu"
        FMS_UNAVAILABLE       = "FMS_UNAVAILABLE",       "FMS indisponible"
        CERTIFIED_FMS         = "CERTIFIED_FMS",         "Certifié FMS"
        CERTIFIED_SENELEC     = "CERTIFIED_SENELEC",     "Certifié Senelec"
        NEEDS_REVIEW          = "NEEDS_REVIEW",          "À analyser"
        # ✅ v4 — alerte mesure (prioritaire sur Analyses FMS/Senelec)
        MESURE_A_VERIFIER     = "MESURE_A_VERIFIER",     "Mesure à vérifier"

    class CertifiedByRule(models.TextChoices):
        FMS_PERIODE  = "FMS_PERIODE",  "Ratio Grid / période"   # Grid primaire
        FMS_30J      = "FMS_30J",      "Ratio Grid / 30 jours"  # Grid primaire
        ACM_PERIODE  = "ACM_PERIODE",  "Ratio ACM / période"    # ACM fallback
        ACM_30J      = "ACM_30J",      "Ratio ACM / 30 jours"   # ACM fallback
        HISTO_3MOIS  = "HISTO_3MOIS",  "Historique 3 mois"

    # ── Relations ────────────────────────────
    cert_batch = models.ForeignKey(
        CertificationBatch,
        on_delete=models.CASCADE,
        related_name="results",
    )
    invoice = models.ForeignKey(
        SonatelInvoice,
        on_delete=models.CASCADE,
        related_name="certifications",
    )
    site = models.ForeignKey(
        "core.Site",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="certification_results",
    )

    # ── Statut & audit ───────────────────────
    status      = models.CharField(
        max_length=32, choices=Status.choices,
        default=Status.PENDING_CERTIFICATION, db_index=True,
    )
    computed_at = models.DateTimeField(auto_now=True)

    # ── Étape 4 : données facture normalisées ─
    conso_facturee_periode = models.DecimalField(**DEC)
    nb_jours_facturation   = models.IntegerField(null=True, blank=True)
    conso_facturee_30j     = models.DecimalField(**DEC)

    # ── Étape 5 : données FMS (Grid primaire) ─
    fms_available           = models.BooleanField(default=False)
    conso_fms_periode       = models.DecimalField(**DEC)
    conso_fms_30j           = models.DecimalField(**DEC)
    fms_last_complete_month = models.DateField(null=True, blank=True)
    fms_error               = models.TextField(null=True, blank=True)

    # ── Étape 6 : historique Senelec interne ─
    histo_last_conso = models.DecimalField(**DEC)
    histo_3mois_avg  = models.DecimalField(**DEC)

    # ── Étape 7A : ratios FMS ────────────────
    ratio_fms_periode = models.DecimalField(**DEC)
    ratio_fms_30j     = models.DecimalField(**DEC)
    ratio_histo_3mois = models.DecimalField(**DEC)

    # ── Étape 7B : double-check ACM (fallback) ──
    acm_available           = models.BooleanField(default=False)
    estim_conso_acm_periode = models.DecimalField(**DEC)
    estim_conso_acm_30j     = models.DecimalField(**DEC)
    ratio_acm_periode       = models.DecimalField(**DEC)
    ratio_acm_30j           = models.DecimalField(**DEC)
    acm_error               = models.TextField(null=True, blank=True)

    # ── Étape 9 — Alerte mesure v4 ────────────
    # TRUE si Conso_Facturée < 50% de Conso_FMS OU Conso_Histo
    flag_mesure_alert = models.BooleanField(default=False, db_index=True)

    # ── Étape 10 : cohérence montant facture ──
    montant_htva_calcule  = models.DecimalField(**DEC)
    variation_montant_pct = models.DecimalField(**DEC)
    montant_coherent      = models.BooleanField(null=True, blank=True)
    montant_check_error   = models.TextField(null=True, blank=True)

    certified_by_rule = models.CharField(
        max_length=16,
        choices=CertifiedByRule.choices,
        null=True, blank=True,
    )

    class Meta:
        ordering = ["-computed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["cert_batch", "invoice"],
                name="uniq_certresult_batch_invoice",
            )
        ]
        indexes = [
            models.Index(fields=["cert_batch", "status"]),
            models.Index(fields=["site", "status"]),
            models.Index(fields=["status"]),
            models.Index(fields=["flag_mesure_alert"]),  # ✅ v4
        ]

    def __str__(self):
        return (
            f"CertResult | {self.invoice.numero_facture} | "
            f"site={self.site.site_id if self.site else 'N/A'} | "
            f"{self.status}"
        )

    @property
    def is_terminal(self) -> bool:
        return self.status not in (self.Status.PENDING_CERTIFICATION,)

    @property
    def is_certified(self) -> bool:
        return self.status in (
            self.Status.CERTIFIED_FMS,
            self.Status.CERTIFIED_SENELEC,
        )


class EfmsConnectionLog(models.Model):

    class Status(models.TextChoices):
        SUCCESS    = "SUCCESS",    "Succès"
        TIMEOUT    = "TIMEOUT",    "Timeout"
        VPN_DOWN   = "VPN_DOWN",   "VPN indisponible"
        AUTH_ERROR = "AUTH_ERROR", "Erreur auth"
        SQL_ERROR  = "SQL_ERROR",  "Erreur SQL"

    cert_batch = models.ForeignKey(
        CertificationBatch,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="efms_logs",
    )
    attempted_at = models.DateTimeField(auto_now_add=True)
    status       = models.CharField(max_length=16, choices=Status.choices, db_index=True)
    duration_ms  = models.IntegerField(null=True, blank=True)
    host         = models.CharField(max_length=64, default="172.30.0.149")
    query        = models.CharField(max_length=512, null=True, blank=True)
    error        = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-attempted_at"]
        indexes = [
            models.Index(fields=["status", "attempted_at"]),
            models.Index(fields=["cert_batch"]),
        ]

    def __str__(self):
        return f"eFMS [{self.status}] {self.attempted_at:%Y-%m-%d %H:%M:%S} ({self.duration_ms}ms)"