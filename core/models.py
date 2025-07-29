from django.db import models

# Create your models here.
from django.db import models

class Site(models.Model):
    ZONE_CHOICES = [
        ('DKR', 'Dakar'),
        ('THS', 'Thiès'),
        ('NDM', 'Ndian'),
        ('KDG', 'Kédougou'),
        ('DBL', 'Diourbel'),
        ('KLK', 'Kolda'),
        ('MBR', 'Matam'),
        ('ZIG', 'Ziguinchor'),
        ('BKL', 'Bakel'),
        ('STL', 'Saint-Louis'),
        ('TMB', 'Tambacounda'),
        ('KLD', 'Kaolack'),
        ('ORG', 'Oriental'),
    ]

    COUNTRY_CHOICES = [
        ('sen', 'Sénégal'),
        ('civ', 'Côte d’Ivoire'),
        ('gin', 'Guinée'),
        ('mli', 'Mali'),
        ('bfa', 'Burkina Faso'),
        ('tgo', 'Togo'),
        ('ben', 'Bénin'),
        ('nga', 'Nigeria'),
        ('gha', 'Ghana'),
        ('cma', 'Cameroon'),
        ('cog', 'Congo'),
        ('gab', 'Gabon'),
        ('cod', 'Congo Démocratique'),
        ('tcd', 'Tchad'),
        ('cmr', 'Cameroun'),
        ('mar', 'Maroc'),
        ('tun', 'Tunisie'),
        ('dji', 'Djibouti'),
        ('eth', 'Ethiopie'),
        ('ken', 'Kenya'),
        ('uga', 'Ouganda'),
        ('rwa', 'Rwanda'),
        ('zmb', 'Zambie'),
        ('zaf', 'Afrique du Sud'),
        ('moz', 'Mozambique'),
        ('ang', 'Angola'),
        ('nam', 'Namibie'),
        ('zwi', 'Zimbabwe'),
        ('tza', 'Tanzanie'),
        ('mwi', 'Malawi'),
        ('sud', 'Soudan'),
        ('ssd', 'Soudan du Sud'),
        ('ery', 'Erythrée'),
        ('som', 'Somalie'),
        ('uga', 'Ouganda'),
        ('cog', 'Congo'),
        ('gab', 'Gabon'),
        ('cod', 'Congo Démocratique'),
        ('tcd', 'Tchad'),
        ('cmr', 'Cameroun'),
        ('mar', 'Maroc'),
        ('tun', 'Tunisie'),
        ('dji', 'Djibouti'),
        ('eth', 'Ethiopie'),
        ('ken', 'Kenya'),
        ('uga', 'Ouganda'),
        ('rwa', 'Rwanda'),
        ('zmb', 'Zambie'),
        ('zaf', 'Afrique du Sud'),
        ('moz', 'Mozambique'),
        ('ang', 'Angola'),
        ('nam', 'Namibie'),
        ('zwi', 'Zimbabwe'),
        ('tza', 'Tanzanie'),
    ]

    site_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    is_new = models.BooleanField(default=False)
    installation_date = models.DateField(null=True, blank=True)
    activation_date = models.DateField(null=True, blank=True)
    is_billed = models.BooleanField(default=True)

    real_typology = models.CharField(max_length=100, null=True, blank=True)
    contratual_typology = models.CharField(max_length=100, null=True, blank=True)
    billing_typology = models.CharField(max_length=100, null=True, blank=True)
    power_kw = models.IntegerField(null=True, blank=True)

    batch_aktivco = models.CharField(max_length=100, null=True, blank=True)
    batch_operational = models.CharField(max_length=100, null=True, blank=True)

    zone = models.CharField(max_length=3, choices=ZONE_CHOICES, null=True, blank=True)
    country = models.CharField(max_length=3, choices=COUNTRY_CHOICES, default='sen')

    def __str__(self):
        return f"{self.site_id} - {self.name}"
