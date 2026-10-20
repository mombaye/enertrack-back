"""
Rattrapage : applique à tous les sites de Gestion des sites la synchronisation automatique
(core/site_sync.py) — scope recalculé, liens contrat → site créés depuis le numéro de contrat (y compris
variantes de format : zéros de tête, espaces), factures Senelec déjà importées rattachées au site,
certifications restées « Contrat inconnu » relancées. Termine par un diagnostic des factures encore
non rattachées, avec la cause pour chaque contrat.

    python manage.py sync_sites_billing            # tous les sites
    python manage.py sync_sites_billing --site DKR_2834
    python manage.py sync_sites_billing --dry-run  # bilan sans rien modifier
    python manage.py sync_sites_billing --csv /mnt/Suivi_carburant_prod/factures_non_rattachees.csv
"""
import csv as csvlib

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q

from core.models import Site
from core.site_sync import contract_key, derive_scope, refresh_unknown_certifications, site_contracts, sync_site_contracts


class Command(BaseCommand):
    help = "Synchronise scope, contrats, factures et certifications de tous les sites de Gestion des sites."

    def add_arguments(self, parser):
        parser.add_argument("--site", action="append", default=[])
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--csv", default=None, help="Liste complète des contrats non rattachés (cause, sites, factures)")

    def handle(self, *args, site, dry_run, csv, **opts):
        from billing.models import ContractSiteLink

        qs = Site.objects.all()
        if site:
            qs = qs.filter(site_id__in=site)
        total = {"sites": 0, "scope_modifies": 0, "contrats": 0, "liens_crees": 0, "liens_reaffectes": 0,
                 "factures_rattachees": 0, "certifications_relancees": 0}
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
            if not dry_run and not site:
                total["certifications_relancees"] += refresh_unknown_certifications()
            if dry_run:
                transaction.set_rollback(True)
        w = self.stdout.write
        w(("DRY RUN — aucune modification. " if dry_run else "") + " · ".join(f"{k} : {v}" for k, v in total.items()))
        self.diagnostic(csv)

    def diagnostic(self, csv_path):
        """Factures encore sans site (cause par contrat) et factures rattachées mais hors périmètre Suivi facturation."""
        from billing.models import ContractSiteLink, SonatelInvoice
        from certification.models import CertificationResult

        w = self.stdout.write
        by_key: dict[str, set[str]] = {}
        for sid, raw in Site.objects.exclude(contract_number__isnull=True).exclude(contract_number="").values_list("site_id", "contract_number"):
            for c in site_contracts(type("S", (), {"contract_number": raw})()):
                k = contract_key(c)
                if k:
                    by_key.setdefault(k, set()).add(sid)
        links = {contract_key(c): sid for c, sid in ContractSiteLink.objects.values_list("numero_compte_contrat", "site__site_id")}
        rows = (SonatelInvoice.objects.filter(site__isnull=True).values("numero_compte_contrat")
                .annotate(n=Count("id"), payees=Count("id", filter=Q(payment_status="PAID"))).order_by("-n"))
        causes: dict[str, list] = {"CONTRAT_AMBIGU": [], "CONTRAT_ABSENT_GESTION_SITES": [], "CONTRAT_LIE_NON_RATTACHE": []}
        for r in rows:
            k = contract_key(r["numero_compte_contrat"])
            sites = sorted(by_key.get(k, set()))
            if len(sites) > 1:
                cause = "CONTRAT_AMBIGU"
            elif sites or (k in links):
                cause = "CONTRAT_LIE_NON_RATTACHE"
            else:
                cause = "CONTRAT_ABSENT_GESTION_SITES"
            causes[cause].append((r["numero_compte_contrat"], r["n"], r["payees"], ", ".join(sites) or links.get(k) or ""))
        total_inv = sum(x[1] for v in causes.values() for x in v)
        total_paid = sum(x[2] for v in causes.values() for x in v)
        w(self.style.MIGRATE_HEADING(f"\nFactures encore sans site : {total_inv} (dont {total_paid} payées)"))
        labels = {"CONTRAT_AMBIGU": "contrat saisi sur plusieurs fiches de Gestion des sites (à corriger sur la fiche)",
                  "CONTRAT_ABSENT_GESTION_SITES": "contrat absent de toutes les fiches de Gestion des sites",
                  "CONTRAT_LIE_NON_RATTACHE": "contrat connu mais factures non rattachées (relancer cette commande)"}
        for cause, items in causes.items():
            w(f"  {cause} — {labels[cause]} : {len(items)} contrat(s), {sum(x[1] for x in items)} facture(s), "
              f"{sum(x[2] for x in items)} payée(s)")
            for c, n, p, sids in items[:10]:
                w(f"     {c} : {n} facture(s), {p} payée(s){(' — sites : ' + sids) if sids else ''}")
        out_scope = (SonatelInvoice.objects.filter(site__isnull=False)
                     .exclude(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)
                     .values("site__site_id", "site__invoice_payment", "site__grid_fee")
                     .annotate(n=Count("id"), payees=Count("id", filter=Q(payment_status="PAID"))).order_by("-payees", "-n"))
        out_scope = list(out_scope)
        w(self.style.MIGRATE_HEADING(f"\nSites rattachés mais hors périmètre Suivi facturation (paiement ≠ Aktivco ou "
                                     f"redevance Grid ≠ Oui) : {len(out_scope)} site(s), "
                                     f"{sum(x['payees'] for x in out_scope)} facture(s) payée(s)"))
        for x in out_scope[:15]:
            w(f"     {x['site__site_id']} : paiement « {x['site__invoice_payment'] or '—'} », redevance Grid "
              f"{ {True: 'Oui', False: 'Non', None: '—'}[x['site__grid_fee']] } — {x['n']} facture(s), {x['payees']} payée(s)")
        unknown = CertificationResult.objects.filter(status=CertificationResult.Status.UNKNOWN_CONTRACT)
        w(self.style.MIGRATE_HEADING(f"\nCertifications « Contrat inconnu » restantes : {unknown.count()} "
                                     f"(dont facture désormais rattachée : {unknown.filter(invoice__site__isnull=False).count()})"))
        if csv_path:
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                wr = csvlib.writer(f, delimiter=";")
                wr.writerow(["type", "contrat_ou_site", "factures", "payees", "detail"])
                for cause, items in causes.items():
                    for c, n, p, sids in items:
                        wr.writerow([cause, c, n, p, sids])
                for x in out_scope:
                    wr.writerow(["SITE_HORS_PERIMETRE_SUIVI", x["site__site_id"], x["n"], x["payees"],
                                 f"paiement={x['site__invoice_payment']}, redevance_grid={x['site__grid_fee']}"])
            w(f"\nListe complète : {csv_path}")
