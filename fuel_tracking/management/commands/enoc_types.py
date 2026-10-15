"""
Dictionnaire réel des types d'opération ENOC (FuelEnocMovement) et classification versionnée.

    python manage.py enoc_types                                   # dictionnaire + classification courante + suggestion
    python manage.py enoc_types --set "AJOUT" AJOUT --reason "Validé avec Ops le 15/10"
"""
import json

from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services.stock_enoc import enoc_type_dictionary, set_enoc_mapping, sync_enoc_to_registry


class Command(BaseCommand):
    help = "Types ENOC : dictionnaire et classification administrable."

    def add_arguments(self, parser):
        parser.add_argument("--set", nargs=2, metavar=("VALEUR_BRUTE", "CATEGORIE"))
        parser.add_argument("--reason", default=None)

    def handle(self, *args, set=None, reason=None, **opts):
        if set:
            try:
                m = set_enoc_mapping(set[0], set[1], None, reason)
            except ValueError as e:
                raise CommandError(str(e))
            self.stdout.write(f"{m.raw_value} → {m.category} (version {m.version})")
            self.stdout.write(json.dumps(sync_enoc_to_registry(), ensure_ascii=False, default=str))
            return
        for row in enoc_type_dictionary():
            self.stdout.write(json.dumps(row, ensure_ascii=False, default=str))
