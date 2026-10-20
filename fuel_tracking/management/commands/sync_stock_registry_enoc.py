"""
Registre stock — projette les mouvements ENOC déjà synchronisés (FuelEnocMovement, connecteur existant)
en mouvements et relevés du registre. Aucune nouvelle connexion ENOC. Idempotent.

    python manage.py sync_stock_registry_enoc
    python manage.py sync_stock_registry_enoc --rapport   # volumes d'intégration eGrid seulement (lecture seule)
    python manage.py sync_stock_registry_enoc --audit-eydon   # taux de remplissage des champs livraison
"""
import json

from django.core.management.base import BaseCommand

from fuel_tracking.services.enoc_canonical import integration_report
from fuel_tracking.services.stock_enoc import audit_enoc_delivery_fields, check_transfer_balance, sync_enoc_to_registry


class Command(BaseCommand):
    help = "ENOC (FuelEnocMovement) → registre stock ; --audit-eydon pour l'audit des champs livraison."

    def add_arguments(self, parser):
        parser.add_argument("--audit-eydon", action="store_true")
        parser.add_argument("--rapport", action="store_true", help="Volumes d'intégration eGrid (lecture seule)")

    def handle(self, *args, audit_eydon, rapport=False, **opts):
        if rapport:
            self.stdout.write(json.dumps(integration_report(), ensure_ascii=False, indent=2, default=str))
            return
        if audit_eydon:
            res = audit_enoc_delivery_fields()
            total = res["total"] or 1
            self.stdout.write(f"Mouvements ENOC synchronisés : {res['total']} (dont {res['avec_operation_realisee']} avec opération réalisée)")
            for k, v in res["champs_renseignes"].items():
                self.stdout.write(f"  {k:<18} {v:>6} / {res['total']}  ({100 * v / total:.0f} %)")
            self.stdout.write(json.dumps({k: res[k] for k in ("types_operation", "statuts", "fournisseurs", "cles_mongo_observees")},
                                         ensure_ascii=False, indent=2))
            return
        stats = sync_enoc_to_registry()
        stats["integration_egrid"] = integration_report()
        bad = check_transfer_balance()
        stats["controle_transferts"] = "OK (somme des impacts = 0 pour chaque transfer_id)" if not bad else bad[:20]
        self.stdout.write(json.dumps(stats, ensure_ascii=False, indent=2, default=str))
