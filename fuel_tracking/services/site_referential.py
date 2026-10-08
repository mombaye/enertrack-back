# fuel_tracking/services/site_referential.py
"""
Base de sites unique : le référentiel commun core.Site (page « Gestion des sites », import du
fichier Proposition de load) délimite les sites affichés dans TOUS les onglets Suivi Carburant
(Dashboard, Suivis Consommations, Contrôle CPH, Stock, Commandes, Estimation), comme dans les
autres modules qui pointent déjà sur core.Site.

Rapprochement des identifiants sans tenir compte de la casse (DkR_2713 = DKR_2713). Un site
connu de Snowflake / ENOC / fichiers Ops mais absent du référentiel n'est plus affiché : il est
compté (`sites_hors_referentiel`) pour être importé dans Gestion des sites. Référentiel vide
(installation neuve) → aucun filtre, pour ne jamais vider les écrans.
"""
from __future__ import annotations


def referential_upper_ids() -> set[str] | None:
    """Identifiants core.Site en majuscules ; None si le référentiel est vide (pas de filtre)."""
    from core.models import Site

    ids = {s.upper() for s in Site.objects.values_list("site_id", flat=True) if s}
    return ids or None


def restrict(qs, ref: set[str] | None = None, field: str = "site_id"):
    """
    Limite un queryset aux sites du référentiel. Retourne (queryset, nombre de sites hors
    référentiel). Les identifiants du modèle sont rapprochés en majuscules.
    """
    ref = referential_upper_ids() if ref is None else ref
    if ref is None:
        return qs, 0
    present = {s for s in qs.model.objects.values_list(field, flat=True).distinct() if s}
    keep = {s for s in present if s.upper() in ref}
    outside = len({s.upper() for s in present} - ref)
    return qs.filter(**{f"{field}__in": keep}), outside


def in_referential(site_id: str | None, ref: set[str] | None) -> bool:
    return ref is None or (site_id or "").upper() in ref
