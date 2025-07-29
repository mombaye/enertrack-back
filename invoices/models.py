from django.db import models
from core.models import Site

class Facture(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name='factures')

    police_number = models.CharField(max_length=50)
    contrat_number = models.CharField(max_length=50)
    facture_number = models.CharField(max_length=50)
    date_facture = models.DateField()
    date_echeance = models.DateField(null=True, blank=True)

    montant_ht = models.DecimalField(max_digits=12, decimal_places=2)
    montant_tco = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_redevance = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_tva = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_ttc = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_htva = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_energie = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    montant_cosphi = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    date_ai = models.DateField(null=True, blank=True)
    date_ni = models.DateField(null=True, blank=True)

    index_ai_k1 = models.BigIntegerField(null=True, blank=True)
    index_ai_k2 = models.BigIntegerField(null=True, blank=True)
    index_ni_k1 = models.BigIntegerField(null=True, blank=True)
    index_ni_k2 = models.BigIntegerField(null=True, blank=True)

    consommation_kwh = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    rappel_majoration = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    nb_jours = models.IntegerField(null=True, blank=True)
    ps = models.FloatField(null=True, blank=True)
    max_relevee = models.FloatField(null=True, blank=True)

    statut = models.CharField(max_length=50, null=True, blank=True)
    observation = models.TextField(null=True, blank=True)
    prime_fixe = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    conso_reactif = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cos_phi = models.FloatField(null=True, blank=True)

    mois_echeance = models.CharField(max_length=50, null=True, blank=True)
    annee_echeance = models.IntegerField(null=True, blank=True)
    mois_business = models.CharField(max_length=50, null=True, blank=True)
    annee_business = models.IntegerField(null=True, blank=True)

    type_tarif = models.CharField(max_length=50, null=True, blank=True)
    type_compte = models.CharField(max_length=50, null=True, blank=True)
    numero_compteur = models.CharField(max_length=100, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.facture_number} ({self.site.site_id})"
