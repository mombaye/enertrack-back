# financial/management/commands/recompute_financial_evaluations.py
"""
Recalcule automatiquement FinancialEvaluation sur une fenêtre glissante de
mois (par défaut les 3 derniers mois calendaires), à chaque démarrage du
conteneur web (cf. docker-compose.yml).

Nécessaire pour que les correctifs touchant _run_financial_evaluation
(financial/views.py) — ex. le clamp de nb_jours_factures à la fenêtre
calendaire du mois — soient automatiquement repris sur les mois déjà
calculés, sans action manuelle (bouton "Recalculer" dans l'UI) après
chaque déploiement.
"""
from django.core.management.base import BaseCommand

from financial.views import _run_financial_evaluation


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


class Command(BaseCommand):
    help = "Recalcule FinancialEvaluation sur une fenêtre glissante de mois (auto au déploiement)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--months", type=int, default=3,
            help="Nombre de mois à recalculer, en remontant depuis le mois courant (défaut : 3).",
        )

    def handle(self, *args, **options):
        from django.utils import timezone

        months = max(1, int(options["months"]))
        now = timezone.now()

        targets = []
        for i in range(months):
            y, m = shift_month(now.year, now.month, -i)
            targets.append((y, m))
        targets.reverse()  # du plus ancien au plus récent

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  RECALCUL AUTO FinancialEvaluation → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Fenêtre : {months} mois — {targets[0][0]}-{targets[0][1]:02d} → {targets[-1][0]}-{targets[-1][1]:02d}")
        self.stdout.write("═" * 80 + "\n")

        for year, month in targets:
            try:
                result = _run_financial_evaluation(year, month)
                self.stdout.write(
                    f"  {year}-{month:02d} : {result['processed']} site(s) — "
                    f"{result['ok']} OK / {result['nok']} NOK"
                )
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  {year}-{month:02d} : échec — {e}"))

        self.stdout.write(self.style.SUCCESS("\n  Recalcul auto FinancialEvaluation terminé.\n"))
