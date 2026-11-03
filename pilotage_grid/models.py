"""
Pilotage Grid — jeux de données du tableau de bord (facturation Senelec, paiements, sites, redevance, tarifs…).

Chaque jeu est stocké EN BASE, versionné : un import crée toujours une nouvelle version (l'ancienne reste consultable et
restaurable). Le contenu actif est exportable à tout moment (zip JSON) pour sortir la base utilisée.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class PilotageDataset(models.Model):
    class Key(models.TextChoices):
        PAIEMENTS = "paiements", "Paiements (cube, détail des factures, hors scope)"
        SITES = "sites", "Sites et séries mensuelles (synthèse, sites, fiche site, non parvenues)"
        DEMANDES = "demandes", "Sites demandés (consommation mensuelle)"
        EFFICACITE = "efficacite", "Efficacité énergétique"
        TARIFS = "tarifs", "Changements de tarif"
        COSPHI = "cosphi", "cos φ par site et par période (fiche site)"
        REDEVANCE = "redevance", "Redevance face aux factures Senelec AKTIVCO"

    key = models.CharField(max_length=24, choices=Key.choices, db_index=True)
    version = models.PositiveIntegerField()
    is_active = models.BooleanField(default=False, db_index=True)
    data = models.JSONField(help_text="Contenu complet du jeu de données (structure attendue par l'interface).")
    checksum = models.CharField(max_length=64, db_index=True)
    size_bytes = models.PositiveBigIntegerField(default=0)
    row_counts = models.JSONField(default=dict, blank=True, help_text="Nombre de lignes par bloc (traçabilité).")
    source = models.CharField(max_length=32, default="import", help_text="seed-html | import | restore")
    source_file = models.CharField(max_length=255, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    imported_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    imported_by_label = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "pilotage_grid_dataset"
        ordering = ["key", "-version"]
        constraints = [
            models.UniqueConstraint(fields=["key", "version"], name="uniq_pilotage_dataset_version"),
            models.UniqueConstraint(fields=["key"], condition=models.Q(is_active=True), name="uniq_pilotage_dataset_active"),
        ]

    def __str__(self):
        return f"{self.key} v{self.version}{' (actif)' if self.is_active else ''}"
