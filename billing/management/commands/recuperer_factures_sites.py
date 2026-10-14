"""
Récupère, pour chaque site de Gestion des sites, ses factures Senelec déjà importées sur une période
(par défaut janvier → octobre de l'année en cours), pour qu'elles apparaissent dans Suivi Facturation.

Les factures Senelec ne sont pas téléchargées depuis une source externe : elles viennent des fichiers
mensuels « fichier de facturation SONATEL » importés dans l'application. Cette commande :
  1. rattache chaque facture déjà importée au site qui porte son numéro de contrat
     (Gestion des sites → lien contrat → site → factures), et recalcule le scope des sites ;
  2. affiche un bilan mois par mois : factures importées, rattachées à un site, visibles dans
     Suivi Facturation (site Aktivco + redevance Grid), et mois sans aucune facture importée
     (fichier à importer) ;
  3. liste les contrats facturés inconnus de Gestion des sites et les sites en scope sans facture.

    python manage.py recuperer_factures_sites --from 2026-01 --to 2026-10
    python manage.py recuperer_factures_sites --from 2026-01 --to 2026-10 --dry-run
    python manage.py recuperer_factures_sites --from 2026-01 --to 2026-10 --site DKR_2834
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q

from billing.models import ContractSiteLink, MonthlySynthesis, SonatelInvoice
from core.models import Site
from core.site_sync import derive_scope, site_contracts, sync_site_contracts

MOIS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]


def parse_month(value: str) -> tuple[int, int]:
    try:
        y, m = value.split("-")
        y, m = int(y), int(m)
        assert 1 <= m <= 12
        return y, m
    except Exception as exc:
        raise CommandError(f"Mois invalide « {value} » (format AAAA-MM)") from exc


class Command(BaseCommand):
    help = "Rattache à chaque site ses factures Senelec importées (janvier → octobre par défaut) et fait le bilan."

    def add_arguments(self, parser):
        year = date.today().year
        parser.add_argument("--from", dest="from_month", default=f"{year}-01")
        parser.add_argument("--to", dest="to_month", default=f"{year}-10")
        parser.add_argument("--site", action="append", default=[], help="Site(s) précis (répétable)")
        parser.add_argument("--dry-run", action="store_true", help="Bilan seulement, aucune modification")

    def handle(self, *args, from_month, to_month, site, dry_run, **opts):
        ys, ms = parse_month(from_month)
        ye, me = parse_month(to_month)
        months = []
        y, m = ys, ms
        while (y, m) <= (ye, me):
            months.append((y, m))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        if not months:
            raise CommandError("--from doit précéder --to")
        w = self.stdout.write

        # 1. Rattachement contrat → site → factures pour chaque site de Gestion des sites.
        sites = Site.objects.all()
        if site:
            sites = sites.filter(site_id__in=site)
        totals = {"sites": 0, "avec_contrat": 0, "scope_modifies": 0, "liens_crees": 0,
                  "liens_reaffectes": 0, "factures_rattachees": 0}
        with transaction.atomic():
            for s in sites.iterator():
                totals["sites"] += 1
                if site_contracts(s):
                    totals["avec_contrat"] += 1
                scope = derive_scope(s)
                if scope and scope != s.scope_status:
                    totals["scope_modifies"] += 1
                    Site.objects.filter(pk=s.pk).update(scope_status=scope)
                for k, v in sync_site_contracts(s).items():
                    if k in totals:
                        totals[k] += v
            if dry_run:
                transaction.set_rollback(True)
        w(self.style.MIGRATE_HEADING(("[DRY RUN] " if dry_run else "") + "1. Rattachement des factures aux sites"))
        w("   " + " · ".join(f"{k} : {v}" for k, v in totals.items()))

        # 2. Bilan mois par mois sur la période.
        period_q = Q()
        for y, m in months:
            period_q |= Q(year=y, month=m)
        syn = MonthlySynthesis.objects.filter(period_q)
        if site:
            syn = syn.filter(source__site__site_id__in=site)
        rows = {(r["year"], r["month"]): r for r in syn.values("year", "month").annotate(
            factures=Count("source", distinct=True),
            rattachees=Count("source", distinct=True, filter=Q(source__site__isnull=False)),
            visibles=Count("source", distinct=True, filter=Q(source__site__invoice_payment__iexact="Aktivco",
                                                             source__site__grid_fee=True)),
            sites=Count("source__site", distinct=True, filter=Q(source__site__invoice_payment__iexact="Aktivco",
                                                               source__site__grid_fee=True)),
        )}
        w(self.style.MIGRATE_HEADING("\n2. Factures par mois (importées · rattachées à un site · visibles dans Suivi Facturation)"))
        missing = []
        for y, m in months:
            r = rows.get((y, m))
            if not r:
                missing.append(f"{MOIS[m - 1]} {y}")
                w(self.style.WARNING(f"   {MOIS[m - 1]:>6} {y} : aucune facture importée — importer le fichier de facturation SONATEL de ce mois"))
            else:
                w(f"   {MOIS[m - 1]:>6} {y} : {r['factures']:>5} importées · {r['rattachees']:>5} rattachées · "
                  f"{r['visibles']:>5} visibles ({r['sites']} sites)")

        # 3. Ce qui reste à corriger.
        if not site:
            known = set(ContractSiteLink.objects.values_list("numero_compte_contrat", flat=True))
            orphans = (SonatelInvoice.objects.filter(site__isnull=True, months__in=syn)
                       .exclude(numero_compte_contrat__in=known)
                       .values_list("numero_compte_contrat", flat=True).distinct())
            orphans = sorted(set(orphans))
            w(self.style.MIGRATE_HEADING("\n3. À corriger"))
            w(f"   Contrats facturés sur la période mais inconnus de Gestion des sites : {len(orphans)}"
              + (" (saisir le numéro de contrat sur la fiche du site, puis relancer cette commande)" if orphans else ""))
            if orphans:
                w("     " + ", ".join(orphans[:40]) + (" …" if len(orphans) > 40 else ""))
            in_scope = Site.objects.filter(invoice_payment__iexact="Aktivco", grid_fee=True)
            billed = set(syn.filter(source__site__isnull=False).values_list("source__site_id", flat=True))
            without = sorted(s.site_id for s in in_scope.only("site_id", "pk") if s.pk not in billed)
            w(f"   Sites en scope (Aktivco + redevance Grid) sans aucune facture sur la période : {len(without)}"
              + (" (numéro de contrat absent ou erroné sur la fiche du site ?)" if without else ""))
            if without:
                w("     " + ", ".join(without[:40]) + (" …" if len(without) > 40 else ""))
        if missing:
            w(self.style.WARNING(f"\nMois sans facture importée : {', '.join(missing)}"))
        w(self.style.SUCCESS("\nTerminé." if not dry_run else "\nDRY RUN terminé — aucune modification enregistrée."))
