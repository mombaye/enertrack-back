# fuel_tracking/management/commands/import_commande_synthese.py
"""
Importe la feuille "Synthèse Commande" du fichier Excel mensuel
"Commande FUEL ESCO SENEGAL <mois> <année>" dans FuelCommandeSynthese.

Voir fuel_tracking/services/commande_synthese_import.py pour la logique de
parsing (partagée avec l'endpoint d'upload FuelCommandeSyntheseImportView,
utilisé par le bouton "Importer" du frontend).

Usage:
    docker compose exec web python manage.py import_commande_synthese --file "data_imports/Commande FUEL ESCO SENEGAL Aout 2026_Draft 1.xlsb"
    docker compose exec web python manage.py import_commande_synthese --file "..." --dry-run
"""
from django.core.management.base import BaseCommand, CommandError

from fuel_tracking.models import FuelCommandeSynthese
from fuel_tracking.services.commande_synthese_import import (
    CommandeSyntheseImportError,
    parse_commande_synthese,
)


class Command(BaseCommand):
    help = 'Importe la feuille "Synthèse Commande" (.xlsb/.xlsx) dans FuelCommandeSynthese'

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Chemin du fichier (relatif au conteneur)")
        parser.add_argument("--month-year", help="Forcer le mois courant au format YYYY-MM (sinon déduit du fichier)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        path = options["file"]
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  IMPORT SYNTHÈSE COMMANDE FUEL")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Lecture : {path}")

        try:
            objects, month_year, prev_ym = parse_commande_synthese(
                path, month_year=options.get("month_year"), log=self.stdout.write
            )
        except (CommandeSyntheseImportError, FileNotFoundError) as e:
            raise CommandError(str(e))

        self.stdout.write(f"\n  Lignes importées : {len(objects)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("\n  DRY RUN — aucune donnée écrite.\n"))
            return

        FuelCommandeSynthese.objects.filter(month_year=month_year).delete()
        FuelCommandeSynthese.objects.bulk_create(objects, batch_size=500)

        self.stdout.write(self.style.SUCCESS(
            f"\n  {len(objects)} lignes importées dans FuelCommandeSynthese pour {month_year}.\n"
        ))
