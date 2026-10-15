"""
Audit des champs disponibles pour le profil énergétique (Environnement, Raccordement, GE, Solaire) :
source, table, colonne, valeurs, couverture, règle de normalisation, valeurs non reconnues.

    python manage.py audit_site_energy_fields
"""
import json

from django.core.management.base import BaseCommand

from fuel_tracking.services.site_energy_profile import audit_energy_fields, energy_profiles


class Command(BaseCommand):
    help = "Audit des sources du profil énergétique des sites."

    def handle(self, *args, **opts):
        for row in audit_energy_fields():
            self.stdout.write(json.dumps(row, ensure_ascii=False, default=str))
        counts = {}
        for p in energy_profiles().values():
            counts[p["profil"]] = counts.get(p["profil"], 0) + 1
        self.stdout.write("Profils calculés : " + json.dumps(counts, ensure_ascii=False))
