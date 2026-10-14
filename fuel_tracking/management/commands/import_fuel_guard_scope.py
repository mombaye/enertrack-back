"""
Registre stock — périmètre de gardiennage (fuel_guard_scope) depuis un CSV / XLSX :
colonnes site_id ; provider (LOLIFORCE | SCSS) ; active_from (JJ/MM/AAAA) ; active_to (optionnel) ; comment.
Aucun site n'est affecté à un prestataire par défaut : seuls les sites listés le sont.

    python manage.py import_fuel_guard_scope --file data_imports/perimetre_gardiennage.csv --dry-run
"""
import csv
import io
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel_tracking.models import FuelGuardScope
from fuel_tracking.services.stock_import_files import iter_sheets, parse_date
from fuel_tracking.services.stock_registry import SiteResolver, log_correction


class Command(BaseCommand):
    help = "Importe le périmètre de gardiennage (site → Loliforce / SCSS, période)."

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, file, dry_run, **opts):
        path = Path(file)
        if not path.is_file():
            raise CommandError(f"Fichier introuvable : {path}")
        data = path.read_bytes()
        if path.suffix.lower() == ".csv":
            text = data.decode("utf-8-sig")
            delim = ";" if text.count(";") >= text.count(",") else ","
            rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        else:
            rows = [list(r) for _, it in iter_sheets(data, path.name) for r in it]
        header = [str(h or "").strip().lower() for h in rows[0]]
        resolver = SiteResolver()
        created, errors = 0, []
        with transaction.atomic():
            for n, r in enumerate(rows[1:], start=2):
                rec = dict(zip(header, r))
                site = resolver.resolve(rec.get("site_id"))
                provider = str(rec.get("provider") or "").strip().upper().replace(" ", "")
                start = parse_date(rec.get("active_from"))
                end = parse_date(rec.get("active_to")) if rec.get("active_to") else None
                if not site or provider not in ("LOLIFORCE", "SCSS") or not start:
                    errors.append(f"ligne {n} : site={rec.get('site_id')!r} provider={rec.get('provider')!r} début={rec.get('active_from')!r}")
                    continue
                obj, was_created = FuelGuardScope.objects.get_or_create(site_id=site, provider=provider, active_from=start,
                                                                        defaults={"active_to": end, "comment": rec.get("comment") or None})
                if was_created:
                    created += 1
                    log_correction("GUARD_SCOPE", obj.pk, "CREATE", f"Import {path.name}", site_id=site, new=f"{provider} {start}→{end or '…'}",
                                   author_label=f"import {path.name}")
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(("DRY RUN — " if dry_run else "") + f"{created} périmètre(s) créé(s), {len(errors)} ligne(s) en erreur.")
        for e in errors[:50]:
            self.stdout.write("  " + e)
