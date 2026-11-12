"""
État des sources du module Carburant (LECTURE SEULE : lit uniquement les journaux de synchro et d'import de la plateforme).

    python manage.py check_fuel_sources            # tableau lisible
    python manage.py check_fuel_sources --strict   # code retour 1 si une source n'est pas OK (supervision)
"""
from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.services.source_status import OK, PARTIEL, sources_status


class Command(BaseCommand):
    help = "État de chaque source carburant (Snowflake, ENOC, fichiers) : OK, PARTIEL, RETARD, ECHEC, BLOQUE, JAMAIS."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="Échoue si au moins une source n'est pas OK.")

    def handle(self, *args, **opts):
        st = sources_status()
        for r in st["sources"]:
            style = self.style.SUCCESS if r["etat"] == OK else self.style.WARNING if r["etat"] == PARTIEL else self.style.ERROR
            self.stdout.write(style(f"{r['etat']:<8}") + f" {r['libelle']:<48} dernier succès : {r['dernier_succes'] or '—'}")
            if r["erreur"]:
                self.stdout.write(f"         {r['erreur']}")
        self.stdout.write(f"État global : {st['etat_global']}")
        if opts["strict"] and st["a_traiter"]:
            raise CommandError(f"Sources à traiter : {', '.join(st['a_traiter'])}")
