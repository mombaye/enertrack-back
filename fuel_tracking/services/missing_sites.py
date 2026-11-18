# fuel_tracking/services/missing_sites.py
"""
Sites connus du Suivi Carburant (Snowflake, fichiers Ops, fichiers de clôture) mais absents de « Gestion des sites » (core.Site).

  - preview()        : liste des sites manquants, avec leur nom et la source qui les connaît (lecture seule) ;
  - create_missing() : crée ces sites dans core.Site avec UNIQUEMENT ce que les sources donnent (identifiant, nom) — aucune
                       typologie, zone ou configuration inventée : elles restent vides et se complètent ensuite avec l'import
                       Ops « Proposition de load » (Gestion des sites → Import Sites), qui ne remplace jamais une valeur par du vide.
Action explicite (bouton, rôle validateur) : jamais lancée automatiquement.
"""
from __future__ import annotations


def _candidates() -> dict[str, dict]:
    from fuel_tracking.models import FuelConsommationMonthly as M, FuelImportBatch as FB, FuelImportRow as IR, FuelSiteInventory as INV

    out: dict[str, dict] = {}

    def add(sid, name, source):
        if not sid or not isinstance(sid, str) or "_" not in sid:
            return
        key = sid.strip().upper()
        e = out.setdefault(key, {"site_id": sid.strip(), "nom": None, "sources": []})
        if name and not e["nom"]:
            e["nom"] = str(name).strip()[:255]
        if source not in e["sources"]:
            e["sources"].append(source)

    for sid, name in INV.objects.values_list("site_id", "site_name"):
        add(sid, name, "Snowflake (inventaire SITE_ESCO_CURRENT)")
    last = M.objects.order_by("-month_year").values_list("month_year", flat=True).first()
    if last:
        for sid, name in M.objects.filter(month_year=last).values_list("site_id", "site_name"):
            add(sid, name, f"Suivi conso mensuel ({last})")
    batches = [b.pk for b in FB.objects.filter(source_type="CLOSING") if "annulation" not in (b.summary or {})]
    for r in IR.objects.filter(batch_id__in=batches, validation_status="REJECTED").only("site_id_raw", "raw_payload", "messages"):
        if any((m or {}).get("code") == "UNKNOWN_SITE" for m in (r.messages or [])):
            add(r.site_id_raw, (r.raw_payload or {}).get("nom du site"), "Fichier de clôture (ligne rejetée)")
    return out


def preview() -> dict:
    from core.models import Site

    known = {s.upper() for s in Site.objects.values_list("site_id", flat=True) if s}
    missing = sorted((e for k, e in _candidates().items() if k not in known), key=lambda e: e["site_id"])
    return {"sites": missing, "total": len(missing)}


def create_missing() -> dict:
    """Crée les sites manquants (identifiant + nom seulement). Idempotent : un site déjà présent n'est jamais modifié."""
    from django.db import transaction

    from core.models import Site

    todo = preview()["sites"]
    created = []
    with transaction.atomic():
        for e in todo:
            if Site.objects.filter(site_id__iexact=e["site_id"]).exists():
                continue
            Site.objects.create(site_id=e["site_id"], name=e["nom"] or e["site_id"])
            created.append(e["site_id"])
    return {"crees": len(created), "sites": created,
            "note": "Sites créés avec leur identifiant et leur nom seulement. Compléter typologies et configuration avec l'import Ops "
                    "« Proposition de load » (Gestion des sites → Import Sites). Pour les lignes rejetées d'un fichier de clôture : "
                    "retirer puis réimporter le fichier dans Configuration."}
