"""
Synchronisation automatique d'un site de Gestion des sites (core.Site) avec la facturation :

1. Scope (scope_status) recalculé à chaque enregistrement à partir des données du site
   (même règle que l'import Excel) :
     - « Sites non encore solarisés » = Oui  → OUT_OF_SCOPE
     - Redevance Grid = Non                  → OUT_OF_SCOPE
     - Paiement facture = Aktivco ET Redevance Grid = Oui → IN_SCOPE
     - sinon (données incomplètes)           → valeur existante conservée
2. Numéro(s) de contrat du site → lien contrat → site de la facturation (billing.ContractSiteLink),
   puis rattachement des factures Senelec déjà importées pour ce(s) contrat(s) : Suivi Facturation
   les voit sans ré-import. Gestion des sites fait foi : un contrat lié à un autre site est
   réaffecté à ce site.

Branché sur les signaux pre_save / post_save de core.Site et post_save de billing.ContractSiteLink
(voir core/apps.py) : toute mise à jour (formulaire, import Excel, import Proposition de load, page Liens
contrats, admin) déclenche la synchronisation. Les contrats sont comparés aussi sans zéros de tête ni
séparateurs (Excel perd les zéros de tête), et les certifications restées « Contrat inconnu » sont relancées.
Rattrapage de l'existant : python manage.py sync_sites_billing
"""
from __future__ import annotations

import contextvars
import logging
import re

logger = logging.getLogger(__name__)

_IN_SITE_SYNC = contextvars.ContextVar("in_site_sync", default=False)  # évite le double rattachement par signal
IN_SCOPE = "IN_SCOPE"
OUT_OF_SCOPE = "OUT_OF_SCOPE"


def derive_scope(site) -> str | None:
    """Scope déduit des données du site ; None si les données ne permettent pas de conclure."""
    if site.not_yet_solarized is True:
        return OUT_OF_SCOPE
    if site.grid_fee is False:
        return OUT_OF_SCOPE
    if (site.invoice_payment or "").strip().lower() == "aktivco" and site.grid_fee is True:
        return IN_SCOPE
    return None


def site_contracts(site) -> list[str]:
    """Numéros de contrat du site (un ou plusieurs, séparés par / ; , ou espace), format des factures."""
    raw = str(site.contract_number or "").strip()
    out = []
    for token in re.split(r"[\s/;,|]+", raw):
        token = token.strip()
        if token.endswith(".0"):
            token = token[:-2]
        if token and token not in out:
            out.append(token)
    return out


def contract_key(value) -> str | None:
    """Clé de comparaison tolérante : chiffres seuls, zéros de tête retirés (« 0012345 », « 12-345 », « 12345.0 » → « 12345 »).
    Les fichiers Excel convertissent souvent le contrat en nombre et perdent les zéros de tête."""
    raw = str(value or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    digits = re.sub(r"\D", "", raw)
    return (digits.lstrip("0") or digits) or None


def other_sites_with_contract(site, contract: str) -> list[str]:
    """Autres sites de Gestion des sites dont la fiche porte aussi ce numéro de contrat."""
    from core.models import Site

    out = []
    for other in Site.objects.filter(contract_number__icontains=contract).exclude(pk=site.pk).only("site_id", "contract_number"):
        if contract in site_contracts(other):
            out.append(other.site_id)
    return out


def sync_site_contracts(site) -> dict:
    """
    Crée / réaffecte les liens contrat → site et rattache les factures de ces contrats au site.
    Un contrat saisi sur PLUSIEURS fiches de Gestion des sites est ambigu : ni lien ni facture n'est
    modifié (aucun basculement d'un site à l'autre), il est seulement signalé (stats["ambigus"]).
    """
    token = _IN_SITE_SYNC.set(True)
    try:
        return _sync_site_contracts(site)
    finally:
        _IN_SITE_SYNC.reset(token)


def _sync_site_contracts(site) -> dict:
    from billing.models import ContractSiteLink, SonatelInvoice

    contracts = site_contracts(site)
    stats = {"contrats": len(contracts), "liens_crees": 0, "liens_reaffectes": 0, "factures_rattachees": 0,
             "certifications_relancees": 0, "ambigus": []}
    resolved = []
    for contract in contracts:
        others = other_sites_with_contract(site, contract)
        if others:
            stats["ambigus"].append((contract, [site.site_id, *others]))
            continue
        resolved.append(contract)
        link = ContractSiteLink.objects.filter(numero_compte_contrat=contract).first()
        if link is None:
            ContractSiteLink.objects.create(numero_compte_contrat=contract, site=site)
            stats["liens_crees"] += 1
        elif link.site_id != site.pk:
            logger.info("[site_sync] contrat %s réaffecté de %s à %s (Gestion des sites fait foi)",
                        contract, link.site_id, site.site_id)
            link.site = site
            link.save(update_fields=["site", "last_seen_at"])
            stats["liens_reaffectes"] += 1
    if resolved:
        # Variantes de format présentes dans les factures (zéros de tête perdus, tirets…) : même contrat.
        keys = {contract_key(c) for c in resolved} - {None}
        variants = set()
        if keys:
            for num in (SonatelInvoice.objects.filter(site__isnull=True).exclude(numero_compte_contrat__in=resolved)
                        .values_list("numero_compte_contrat", flat=True).distinct()):
                if contract_key(num) in keys:
                    variants.add(num)
        for num in variants:
            link = ContractSiteLink.objects.filter(numero_compte_contrat=num).first()
            if link is None:
                ContractSiteLink.objects.create(numero_compte_contrat=num, site=site)
                stats["liens_crees"] += 1
            elif link.site_id != site.pk:
                continue  # variante déjà liée à un autre site : jamais déplacée automatiquement
            resolved.append(num)
        ids = list(SonatelInvoice.objects.filter(numero_compte_contrat__in=resolved).exclude(site_id=site.pk)
                   .values_list("id", flat=True))
        if ids:
            stats["factures_rattachees"] = SonatelInvoice.objects.filter(id__in=ids).update(site=site)
            stats["certifications_relancees"] = refresh_unknown_certifications(ids)
    return stats


def relink_contract(link) -> int:
    """Lien contrat → site créé / modifié (page Liens contrats, admin, import) : rattache les factures de ce contrat."""
    from billing.models import SonatelInvoice

    ids = list(SonatelInvoice.objects.filter(numero_compte_contrat=link.numero_compte_contrat)
               .exclude(site_id=link.site_id).values_list("id", flat=True))
    if not ids:
        return 0
    n = SonatelInvoice.objects.filter(id__in=ids).update(site_id=link.site_id)
    refresh_unknown_certifications(ids)
    return n


def refresh_unknown_certifications(invoice_ids=None) -> int:
    """
    Résultats de certification restés « Contrat inconnu » alors que la facture a maintenant un site :
    remis « En attente » avec le site, puis recertifiés (tâche Celery). Les compteurs des lots sont recalculés.
    """
    from django.db import transaction

    from certification.models import CertificationBatch, CertificationResult

    qs = CertificationResult.objects.filter(status=CertificationResult.Status.UNKNOWN_CONTRACT, invoice__site__isnull=False)
    if invoice_ids is not None:
        qs = qs.filter(invoice_id__in=list(invoice_ids))
    rows = list(qs.select_related("invoice").only("id", "cert_batch_id", "invoice__site_id"))
    if not rows:
        return 0
    for r in rows:
        CertificationResult.objects.filter(pk=r.pk).update(site_id=r.invoice.site_id,
                                                           status=CertificationResult.Status.PENDING_CERTIFICATION)
    for batch in CertificationBatch.objects.filter(pk__in={r.cert_batch_id for r in rows}):
        batch.refresh_counters()

    def dispatch(ids=[r.pk for r in rows]):
        try:
            from certification.tasks import certify_single_invoice

            for rid in ids:
                certify_single_invoice.delay(rid)
        except Exception:  # broker indisponible : les résultats restent « En attente », relançables depuis Certification
            logger.exception("[site_sync] recertification non planifiée (%s résultats en attente)", len(ids))

    transaction.on_commit(dispatch)
    return len(rows)


def on_contract_link_post_save(sender, instance, **kwargs):
    if kwargs.get("raw") or _IN_SITE_SYNC.get():
        return
    try:
        relink_contract(instance)
    except Exception:
        logger.exception("[site_sync] rattachement des factures impossible pour le contrat %s", instance.numero_compte_contrat)


def on_site_pre_save(sender, instance, **kwargs):
    scope = derive_scope(instance)
    if scope is not None:
        instance.scope_status = scope


def on_site_post_save(sender, instance, **kwargs):
    if kwargs.get("raw") or not instance.contract_number:
        return
    try:
        sync_site_contracts(instance)
    except Exception:  # la sauvegarde du site ne doit jamais échouer à cause de la facturation
        logger.exception("[site_sync] synchronisation contrat/factures impossible pour %s", instance.site_id)
