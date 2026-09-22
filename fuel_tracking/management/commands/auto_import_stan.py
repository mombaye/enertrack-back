# fuel_tracking/management/commands/auto_import_stan.py
"""
Appelé automatiquement par entrypoint.sh au démarrage du container.

Vérifie si le dernier fichier .xlsx dans data_imports/stan/ a déjà été
importé (via le sentinel .last_import). Si non, lance l'import et met
le sentinel à jour.

Détecter le mois :
  1. Variable d'environnement STAN_IMPORT_MONTH=2026-10 (priorité absolue)
  2. Parsing du nom de fichier ("base sept 26" → 2026-09, "oct 26" → 2026-10)
  3. Mois de la date de modification du fichier (fallback)

Idempotent : relancer docker compose sans changer le fichier Stan n'exécute
rien (sentinel identique → skip immédiat, démarrage non ralenti).
"""
import os
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

STAN_DIR = Path(settings.BASE_DIR) / "data_imports" / "stan"
SENTINEL = STAN_DIR / ".last_import"

MONTH_NAMES = {
    "jan": 1, "fev": 2, "feb": 2, "mar": 3, "avr": 4, "apr": 4,
    "mai": 5, "may": 5, "jun": 6, "jui": 6, "jul": 7, "aou": 8,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_month_from_filename(name: str) -> str | None:
    """'ESCO SN _ base sept 26 validé-GE.xlsx' → '2026-09'."""
    m = re.search(
        r'(jan|fev|feb|mar|avr|apr|mai|may|jun|jui|jul|aou|aug|sep|oct|nov|dec)'
        r'\w*[\s_\-]*(\d{2,4})',
        name.lower(),
    )
    if not m:
        return None
    month_num = MONTH_NAMES.get(m.group(1)[:3])
    if not month_num:
        return None
    year = int(m.group(2))
    if year < 100:
        year += 2000
    return f"{year:04d}-{month_num:02d}"


def _read_sentinel() -> tuple[str, str] | None:
    """Retourne (nom_fichier, YYYY-MM) ou None si absent/corrompu."""
    if not SENTINEL.exists():
        return None
    try:
        parts = SENTINEL.read_text().strip().split("\t")
        if len(parts) == 2:
            return parts[0], parts[1]
    except OSError:
        pass
    return None


def _write_sentinel(filename: str, month: str) -> None:
    SENTINEL.write_text(f"{filename}\t{month}")


class Command(BaseCommand):
    help = (
        "Auto-import du fichier Stan (data_imports/stan/) si nouveau fichier détecté. "
        "Appelé par entrypoint.sh. Passer STAN_IMPORT_MONTH=YYYY-MM dans .env pour "
        "forcer le mois cible (sinon auto-détecté depuis le nom de fichier)."
    )

    def handle(self, *args, **options):
        if not STAN_DIR.exists():
            self.stdout.write("  [Stan] Dossier data_imports/stan/ absent — import ignoré.")
            return

        candidates = sorted(
            STAN_DIR.glob("*.xlsx"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            self.stdout.write("  [Stan] Aucun fichier .xlsx dans data_imports/stan/ — import ignoré.")
            return

        latest = candidates[0]
        sentinel = _read_sentinel()

        if sentinel and sentinel[0] == latest.name:
            self.stdout.write(
                f"  [Stan] {latest.name} déjà importé pour {sentinel[1]} — aucune action."
            )
            return

        # Déterminer le mois cible
        month = os.environ.get("STAN_IMPORT_MONTH", "").strip()
        if not month:
            month = _parse_month_from_filename(latest.name) or ""
        if not re.match(r"^\d{4}-\d{2}$", month):
            dt = datetime.fromtimestamp(latest.stat().st_mtime)
            month = f"{dt.year:04d}-{dt.month:02d}"
            self.stdout.write(
                f"  [Stan] Mois non détecté dans le nom de fichier — "
                f"utilise la date de modification : {month}. "
                f"Définissez STAN_IMPORT_MONTH=YYYY-MM dans .env pour forcer."
            )

        self.stdout.write(f"  [Stan] Nouveau fichier détecté : {latest.name} → import pour {month} ...")
        call_command("import_facturation_par_site", file=str(latest), month=month)
        _write_sentinel(latest.name, month)
        self.stdout.write(
            self.style.SUCCESS(
                f"  [Stan] Import terminé — sentinel mis à jour ({latest.name} | {month})."
            )
        )
