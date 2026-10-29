"""
Tableau de validation terrain : ~10 sites RÉELS d'une période déjà calculée, couvrant les situations clés. LECTURE SEULE.
(Même rapport que le téléchargement « Tableau de validation » dans eGrid.)

    python manage.py fuel_validation_sites --debut 2026-08-01 --fin 2026-08-31 [--nombre 10] [--anonymiser] [--calculer]
"""
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from fuel_tracking.services.stock_reports import ReportError, VALIDATION_COLUMNS, to_csv, validation_table


class Command(BaseCommand):
    help = "Tableau de validation sur ~10 sites réels d'une période déjà calculée (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--debut", required=True)
        parser.add_argument("--fin", required=True)
        parser.add_argument("--nombre", type=int, default=10)
        parser.add_argument("--csv", default=None, help="Chemin de sortie (le même fichier se télécharge dans eGrid).")
        parser.add_argument("--anonymiser", action="store_true")
        parser.add_argument("--calculer", action="store_true", help="(Re)calcule d'abord le bilan de la période (aucune certification).")

    def handle(self, *args, debut, fin, nombre, csv=None, anonymiser, calculer, **opts):
        from fuel_tracking.services import stock_period_balance as SB

        tz = timezone.get_current_timezone()
        start = timezone.make_aware(datetime.combine(datetime.strptime(debut, "%Y-%m-%d").date(), time.min), tz)
        end = timezone.make_aware(datetime.combine(datetime.strptime(fin, "%Y-%m-%d").date(), time(23, 59, 59)), tz)
        if calculer:
            SB.compute_balances(start, end)
        try:
            table, covered, missing = validation_table(start, end, nombre, anonymiser)
        except ReportError as e:
            raise CommandError(str(e))
        w = self.stdout.write
        w(f"Période {start:%d/%m/%Y} → {end:%d/%m/%Y} — {len(table)} sites retenus (lecture seule, rien n'est certifié)\n")
        for r in table:
            w(" | ".join(f"{k}={'—' if v is None else v}" for k, v in r.items()))
        w(f"\nProfils couverts : {covered}")
        w(f"Profils non trouvés sur cette période : {missing or 'aucun'}")
        if csv:
            open(csv, "w", encoding="utf-8").write(to_csv(VALIDATION_COLUMNS, table))
            w(f"CSV : {csv}")
