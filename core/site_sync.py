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

Branché sur les signaux pre_save / post_save de core.Site (voir core/apps.py) : toute mise à jour
(formulaire, import Excel, import Proposition de load, admin) déclenche la synchronisation.
Rattrapage de l'existant : python manage.py sync_sites_billing
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

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
    from billing.models import ContractSiteLink, SonatelInvoice

    contracts = site_contracts(site)
    stats = {"contrats": len(contracts), "liens_crees": 0, "liens_reaffectes": 0, "factures_rattachees": 0,
             "ambigus": []}
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
        stats["factures_rattachees"] = (
            SonatelInvoice.objects.filter(numero_compte_contrat__in=resolved)
            .exclude(site_id=site.pk).update(site=site)
        )
    return stats


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
