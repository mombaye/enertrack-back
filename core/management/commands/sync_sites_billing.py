"""
Rattrapage : applique à tous les sites de Gestion des sites la synchronisation automatique
(core/site_sync.py) — scope recalculé, liens contrat → site créés depuis le numéro de contrat,
factures Senelec déjà importées rattachées au site.

    python manage.py sync_sites_billing            # tous les sites
    python manage.py sync_sites_billing --site DKR_2834
    python manage.py sync_sites_billing --dry-run  # bilan sans rien modifier
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from core.models import Site
from core.site_sync import derive_scope, site_contracts, sync_site_contracts


class Command(BaseCommand):
    help = "Synchronise scope, contrats et factures de tous les sites de Gestion des sites."

    def add_arguments(self, parser):
        parser.add_argument("--site", action="append", default=[])
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, site, dry_run, **opts):
        from billing.models import ContractSiteLink

        qs = Site.objects.all()
        if site:
            qs = qs.filter(site_id__in=site)
        total = {"sites": 0, "scope_modifies": 0, "contrats": 0, "liens_crees": 0, "liens_reaffectes": 0,
                 "factures_rattachees": 0}
        with transaction.atomic():
            for s in qs.iterator():
                total["sites"] += 1
                scope = derive_scope(s)
                if scope and scope != s.scope_status:
                    total["scope_modifies"] += 1
                    if not dry_run:
                        Site.objects.filter(pk=s.pk).update(scope_status=scope)
                if dry_run:
                    contracts = site_contracts(s)
                    total["contrats"] += len(contracts)
                    linked = dict(ContractSiteLink.objects.filter(numero_compte_contrat__in=contracts)
                                  .values_list("numero_compte_contrat", "site_id"))
                    total["liens_crees"] += sum(1 for c in contracts if c not in linked)
                    total["liens_reaffectes"] += sum(1 for c in contracts if c in linked and linked[c] != s.pk)
                    continue
                for k, v in sync_site_contracts(s).items():
                    if k in total:
                        total[k] += v
            if dry_run:
                transaction.set_rollback(True)
        self.stdout.write(("DRY RUN — aucune modification. " if dry_run else "") +
                          " · ".join(f"{k} : {v}" for k, v in total.items()))
