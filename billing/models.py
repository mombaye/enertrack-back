from decimal import Decimal
from django.db import models

DEC = dict(max_digits=18, decimal_places=3, null=True, blank=True)


class ImportBatch(models.Model):
    source_filename = models.CharField(max_length=255)
    imported_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.source_filename} ({self.imported_at:%Y-%m-%d %H:%M})"


class SonatelInvoice(models.Model):
    """Ligne brute issue du fichier Sonatel (une ligne = un contrat / une facture / une période)."""
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")

    # Identifiants & localisation
    numero_compte_contrat = models.CharField(max_length=32, db_index=True)
    partenaire = models.CharField(max_length=255, null=True, blank=True)
    localite = models.CharField(max_length=255, null=True, blank=True)
    arrondissement = models.CharField(max_length=255, null=True, blank=True)
    rue = models.CharField(max_length=255, null=True, blank=True)

    # Facture
    numero_facture = models.CharField(max_length=64, db_index=True)
    date_comptable_facture = models.DateField()

    # Montants principaux
    montant_total_energie = models.DecimalField(**DEC)
    montant_redevance = models.DecimalField(**DEC)
    montant_tco = models.DecimalField(**DEC)
    montant_hors_tva = models.DecimalField(**DEC)
    montant_tva = models.DecimalField(**DEC)
    montant_ttc = models.DecimalField(**DEC)

    # Période
    date_debut_periode = models.DateField()
    date_fin_periode = models.DateField()

    # Index/Conso
    ancien_index_k1 = models.DecimalField(**DEC)
    ancien_index_k2 = models.DecimalField(**DEC)
    nouvel_index_k1 = models.DecimalField(**DEC)
    nouvel_index_k2 = models.DecimalField(**DEC)
    conso_facturee = models.DecimalField(**DEC)

    # Divers utiles
    agence = models.CharField(max_length=128, null=True, blank=True)
    numero_compteur = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "numero_compte_contrat",
                    "numero_facture",
                    "date_debut_periode",
                    "date_fin_periode",
                ],
                name="uniq_sonatel_invoice_row",
            )
        ]
        indexes = [
            models.Index(fields=["numero_compte_contrat", "date_debut_periode", "date_fin_periode"]),
            models.Index(fields=["numero_facture"]),
        ]

    def __str__(self):
        return f"{self.numero_facture} / {self.numero_compte_contrat}"


class MonthlySynthesis(models.Model):
    """Répartition mensuelle (prorata jours) d'une ligne SonatelInvoice."""
    source = models.ForeignKey(
        SonatelInvoice, on_delete=models.CASCADE, related_name="months"
    )

    # repère calendaire du segment
    year = models.IntegerField()
    month = models.IntegerField()

    # infos de période (de la facture *complète*)
    period_start = models.DateField()         # = date_debut_periode (facture)
    period_end = models.DateField()           # = date_fin_periode (facture)
    period_total_days = models.IntegerField() # = (period_end - period_start) + 1

    # jours du SEGMENT pour ce mois (utilisé pour le prorata)
    days_covered = models.IntegerField()

    # valeurs proratisées mensuelles
    conso = models.DecimalField(**DEC)
    montant_energie = models.DecimalField(**DEC)
    montant_ttc = models.DecimalField(**DEC)

    # Copie de clés fonctionnelles pour filtres rapides
    numero_compte_contrat = models.CharField(max_length=32, db_index=True)
    numero_facture = models.CharField(max_length=64, db_index=True)

    class Meta:
        unique_together = ("source", "year", "month")
        indexes = [
            models.Index(fields=["year", "month", "numero_compte_contrat"]),
        ]

    def __str__(self):
        return f"{self.numero_compte_contrat} {self.year}-{self.month:02d}"
