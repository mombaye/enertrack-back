"""
Charge en base les jeux de données initiaux depuis le fichier HTML « Pilotage Grid » (export autonome).

    python manage.py pilotage_seed_from_html --file "Pilotage_Grid_….html" [--dry-run]

Le fichier est LU comme du texte (rien n'est exécuté). Chaque jeu devient une version en base (source « seed-html »).
Relancer avec le même fichier ne crée aucune nouvelle version (somme de contrôle identique).
"""
from django.core.management.base import BaseCommand, CommandError

from pilotage_grid.services import html_extract as X
from pilotage_grid.services import store as S


class Command(BaseCommand):
    help = "Charge les jeux de données Pilotage Grid depuis le fichier HTML d'origine."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, file, dry_run, **opts):
        try:
            with open(file, encoding="utf-8") as fh:
                html = fh.read()
            datasets = X.extract_datasets(html)
        except (OSError, X.ExtractError, ValueError) as e:
            raise CommandError(str(e))
        for key, data in datasets.items():
            try:
                S.validate(key, data)
            except S.DatasetError as e:
                raise CommandError(f"{key} : {e}")
            counts = {k: len(v) for k, v in data.items() if isinstance(v, (list, dict))}
            if dry_run:
                self.stdout.write(f"[dry-run] {key} : {counts}")
                continue
            ds, created = S.save_version(key, data, source="seed-html", source_file=file.rsplit("/", 1)[-1], note="Chargement initial depuis le HTML")
            self.stdout.write(f"{key} : {'version ' + str(ds.version) + ' créée' if created else 'inchangé (v' + str(ds.version) + ')'} — {counts}")
        if not dry_run:
            from pilotage_grid.services import tables

            self.stdout.write(f"Tables relationnelles reconstruites : {tables.rebuild_all()}")
