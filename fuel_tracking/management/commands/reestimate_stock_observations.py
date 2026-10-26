"""
Recalcule la qualité et l'estimation (cuve géométrique sans plaque / cuve proposée au-dessus du seuil) de tous les relevés
non validés humainement, après évaluation des cuves (score de compatibilité). Idempotent ; chaque changement est tracé.

    python manage.py reestimate_stock_observations
"""
import json

from django.core.management.base import BaseCommand

from fuel_tracking.models import FuelStockObservation
from fuel_tracking.services.stock_registry import mark_potential_duplicates, reconvert_site_observations
from fuel_tracking.services.stock_tanks import evaluate_site_tanks


class Command(BaseCommand):
    help = "Score des cuves + qualité / estimation des relevés + doublons potentiels."

    def handle(self, *args, **opts):
        tanks = evaluate_site_tanks()
        sites = sorted(FuelStockObservation.objects.exclude(source_type="SNOWFLAKE").values_list("site_id", flat=True).distinct())
        n = sum(reconvert_site_observations(s) or 0 for s in sites)
        dup = mark_potential_duplicates()
        from django.db.models import Count

        q = dict(FuelStockObservation.objects.exclude(source_type="SNOWFLAKE").values_list("reading_quality_status").annotate(n=Count("id")))
        self.stdout.write(json.dumps({"cuves": tanks, "releves_recalcules": n, "doublons_potentiels_modifies": dup,
                                      "qualite_releves": {k or "NON_QUALIFIE": v for k, v in q.items()}}, ensure_ascii=False, indent=2, default=str))
