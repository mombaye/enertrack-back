# fuel_tracking/services/site_referential.py
"""
Base de sites commune : le référentiel core.Site (page « Gestion des sites », import du fichier
Proposition de load) est la référence de tous les modules. Dans Suivi Carburant, les sites connus
uniquement de Snowflake / ENOC / fichiers Ops restent AFFICHÉS, avec la mention « hors référentiel »
(`hors_referentiel`) et un compteur (`sites_hors_referentiel`) pour les importer dans Gestion des sites.

Rapprochement des identifiants sans tenir compte de la casse (DkR_2713 = DKR_2713). Référentiel vide
(installation neuve) → aucun site n'est signalé.
"""
from __future__ import annotations


def referential_upper_ids() -> set[str] | None:
    """Identifiants core.Site en majuscules ; None si le référentiel est vide."""
    from core.models import Site

    ids = {s.upper() for s in Site.objects.values_list("site_id", flat=True) if s}
    return ids or None


def restrict(qs, ref: set[str] | None = None, field: str = "site_id"):
    """
    Retourne (queryset inchangé, nombre de sites hors référentiel) : tous les sites restent affichés,
    ceux absents de Gestion des sites sont seulement comptés (et signalés ligne par ligne via in_referential).
    """
    ref = referential_upper_ids() if ref is None else ref
    if ref is None:
        return qs, 0
    present = {s.upper() for s in qs.values_list(field, flat=True).distinct() if s}
    return qs, len(present - ref)


def in_referential(site_id: str | None, ref: set[str] | None) -> bool:
    return ref is None or (site_id or "").upper() in ref
