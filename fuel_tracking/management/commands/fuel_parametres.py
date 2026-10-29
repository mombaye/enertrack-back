"""
Valeurs EFFECTIVES des paramètres métier du suivi carburant (celles appliquées par le moteur en production) et cohérence des données estimées.
LECTURE SEULE.

    python manage.py fuel_parametres
"""
import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from fuel_tracking.models import FuelStockObservation as Obs
from fuel_tracking.services.stock_period_balance import effective_parameters


class Command(BaseCommand):
    help = "Affiche les paramètres effectifs du moteur de bilan et les relevés estimés déjà enregistrés."

    def handle(self, *args, **opts):
        w, p = self.stdout.write, effective_parameters()
        w(json.dumps(p, ensure_ascii=False, indent=2))
        modes = {r["estimation_mode"]: r["n"] for r in Obs.objects.filter(estimated_liters__isnull=False).values("estimation_mode").annotate(n=Count("id"))}
        w(f"\nRelevés estimés enregistrés : {modes or 'aucun'}")
        geo = modes.get("CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE", 0)
        if p["ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE"] and not geo:
            w("→ Estimation géométrique ACTIVÉE mais aucun relevé estimé enregistré : lancer `reestimate_stock_observations`.")
        if not p["ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE"] and geo:
            w(f"→ Estimation géométrique DÉSACTIVÉE mais {geo} relevé(s) estimé(s) hérité(s) d'avant le paramètre sont encore enregistrés : lancer "
              "`reestimate_stock_observations` pour les aligner sur le paramètre.")
        if p["ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE"] and not p["ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE_definie_dans_l_environnement"]:
            w("→ Valeur active sans variable d'environnement explicite : fixer ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE=true dans l'environnement du service web.")
