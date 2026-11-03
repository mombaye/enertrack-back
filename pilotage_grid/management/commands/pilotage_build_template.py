"""
Génère le gabarit d'interface (HTML autonome, données remplacées par des références à l'API) à partir du HTML d'origine.

    python manage.py pilotage_build_template --html "Pilotage_Grid_….html" --out ../enertrack-front/public/pilotage-app/index.html
"""
from django.core.management.base import BaseCommand, CommandError

from pilotage_grid.services import html_extract as X


class Command(BaseCommand):
    help = "Génère le gabarit d'interface Pilotage Grid."

    def add_arguments(self, parser):
        parser.add_argument("--html", required=True)
        parser.add_argument("--out", required=True)

    def handle(self, *args, html, out, **opts):
        try:
            with open(html, encoding="utf-8") as fh:
                tpl = X.build_template(fh.read())
        except (OSError, X.ExtractError, ValueError) as e:
            raise CommandError(str(e))
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(tpl)
        self.stdout.write(f"Gabarit écrit : {out} ({len(tpl):,} caractères)")
