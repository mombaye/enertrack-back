"""
Contrôle de santé du calcul CPH (fraîcheur Snowflake, synchro, référentiel, couverture,
plausibilité L/kWh). Code de sortie 1 si une anomalie CRITIQUE est détectée.

    python manage.py check_cph_health
"""
import sys

from django.core.management.base import BaseCommand

from fuel_tracking.services.cph_health import cph_health


class Command(BaseCommand):
    help = "Contrôle de santé du calcul CPH / conso estimée."

    def handle(self, *args, **opts):
        h = cph_health()
        f = h["fraicheur"]
        self.stdout.write(f"Faits Snowflake jusqu'au {f['facts_last_date']} ({f['facts_age_days']} j)")
        for k, v in h["metrics"].items():
            self.stdout.write(f"  {k} : {v}")
        if h["ok"]:
            self.stdout.write(self.style.SUCCESS("OK — aucune anomalie."))
            return
        for i in h["issues"]:
            style = self.style.ERROR if i["niveau"] == "CRITIQUE" else self.style.WARNING
            self.stdout.write(style(f"[{i['niveau']}] {i['code']} : {i['message']}"))
        if h["critique"]:
            sys.exit(1)
