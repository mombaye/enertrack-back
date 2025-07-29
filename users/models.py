from django.db import models

# Create your models here.
from django.contrib.auth.models import AbstractUser
from django.db import models

class CustomUser(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('manager', 'Manager'),
        ('analyst', 'Analyst'),
    )

    COUNTRY_CHOICES = (
        ('sen', 'Sénégal'),
        ('civ', 'Côte d’Ivoire'),
        ('cam', 'cameroun'),
        ('td', 'Tchad'),
        ('bfa', 'Burkina Faso'),
    )

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='analyst')
    pays = models.CharField(max_length=3, choices=COUNTRY_CHOICES, default='sen')

    def __str__(self):
        return self.username
