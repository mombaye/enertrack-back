"""
État de chaque source du module Carburant (LECTURE SEULE de la base de la plateforme, aucune requête Snowflake / ENOC).

Une ligne par source : dernière exécution, dernier succès, âge, seuil et un état parmi
  OK        dernier passage réussi et récent
  PARTIEL   dernier passage réussi mais une sous-source manquait (ex. compteur horaire illisible)
  RETARD    dernier succès plus vieux que le seuil
  ECHEC     dernier passage terminé en échec (message d'erreur exact, tronqué)
  BLOQUE    un passage est « en cours » depuis plus longtemps que le seuil
  JAMAIS    aucune exécution ni aucun fichier enregistré
Seuils réglables par variables d'environnement (valeurs par défaut à valider avec le métier).
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

OK, PARTIEL, RETARD, ECHEC, BLOQUE, JAMAIS = "OK", "PARTIEL", "RETARD", "ECHEC", "BLOQUE", "JAMAIS"
SEVERITE = {OK: 0, PARTIEL: 1, RETARD: 2, BLOQUE: 2, JAMAIS: 2, ECHEC: 3}
WARNING_PREFIX = "AVERTISSEMENT"


def _minutes(name: str, default: int) -> timedelta:
    return timedelta(minutes=int(getattr(settings, name, default)))


def _days(name: str, default: int) -> timedelta:
    return timedelta(days=int(getattr(settings, name, default)))


def _short(msg: str | None) -> str | None:
    if not msg:
        return None
    return " ".join(str(msg).split())[:300]


def _run_sources():
    from fuel_tracking import models as M

    return [
        ("snowflake_conso", "Snowflake · conso mensuelle", M.FuelConsommationSyncRun, _minutes("FUEL_SOURCE_MAX_AGE_CONSO_MIN", 60)),
        ("snowflake_stock", "Snowflake · stock capteur", M.FuelStockSyncRun, _minutes("FUEL_SOURCE_MAX_AGE_STOCK_MIN", 60)),
        ("snowflake_faits", "Snowflake · faits journaliers (heures GE, CPH)", M.FuelDailyFactsSyncRun, _minutes("FUEL_SOURCE_MAX_AGE_FACTS_MIN", 180)),
        ("enoc", "ENOC · livraisons et ponctions", M.FuelEnocSyncRun, _minutes("FUEL_SOURCE_MAX_AGE_ENOC_MIN", 60)),
    ]


FILE_SOURCES = [
    ("scss", "Fichier · relevés SCSS", "SCSS", "FUEL_SOURCE_MAX_AGE_SCSS_DAYS", 8),
    ("loliforce", "Fichier · relevés Loliforce", "LOLIFORCE", "FUEL_GUARD_READING_MAX_AGE_DAYS", 16),
    ("closing", "Fichier · Closing mensuel", "CLOSING", "FUEL_SOURCE_MAX_AGE_CLOSING_DAYS", 45),
    ("target_fuel", "Fichier · target fuel", "TARGET_FUEL", "FUEL_SOURCE_MAX_AGE_TARGET_DAYS", 45),
]


def _run_status(model, max_age: timedelta, now) -> dict:
    runs = model.objects.order_by("-started_at")
    latest = runs.first()
    if latest is None:
        return {"etat": JAMAIS, "derniere_execution": None, "dernier_succes": None, "erreur": None}
    done = runs.exclude(status="RUNNING").first()
    success = runs.filter(status="SUCCESS").first()
    out = {
        "derniere_execution": (done.finished_at or done.started_at).isoformat() if done else None,
        "dernier_succes": (success.finished_at or success.started_at).isoformat() if success else None,
        "erreur": None,
    }
    if latest.status == "RUNNING" and now - latest.started_at > max_age:
        out.update(etat=BLOQUE, erreur=f"Exécution en cours depuis {latest.started_at.isoformat()}")
    elif done is not None and done.status == "FAILED":
        out.update(etat=ECHEC, erreur=_short(done.error_message) or "Échec sans message")
    elif success is None:
        out.update(etat=JAMAIS if done is None else ECHEC)
    elif now - (success.finished_at or success.started_at) > max_age:
        out.update(etat=RETARD)
    elif (getattr(success, "error_message", None) or "").startswith(WARNING_PREFIX):
        out.update(etat=PARTIEL, erreur=_short(success.error_message))
    else:
        out.update(etat=OK)
    return out


def _file_status(source_type: str, max_age: timedelta, now) -> dict:
    from fuel_tracking.models import FuelImportBatch as B

    qs = B.objects.filter(source_type=source_type).order_by("-received_at")
    latest = qs.first()
    if latest is None:
        return {"etat": JAMAIS, "derniere_execution": None, "dernier_succes": None, "erreur": None, "dernier_fichier": None}
    ok = qs.filter(status__in=("ACCEPTED", "VALIDATION_REQUIRED")).first()
    out = {"derniere_execution": latest.received_at.isoformat(), "dernier_succes": ok.received_at.isoformat() if ok else None,
           "erreur": None, "dernier_fichier": latest.file_name}
    if latest.status in ("REJECTED", "FAILED"):
        out.update(etat=ECHEC, erreur=_short(latest.status_message) or latest.status)
    elif ok is None:
        out.update(etat=JAMAIS)
    elif now - ok.received_at > max_age:
        out.update(etat=RETARD)
    else:
        out.update(etat=OK)
    return out


def sources_status(now=None) -> dict:
    now = now or timezone.now()
    rows = []
    for key, label, model, max_age in _run_sources():
        rows.append({"source": key, "libelle": label, "seuil_minutes": int(max_age.total_seconds() // 60), **_run_status(model, max_age, now)})
    for key, label, source_type, setting, default in FILE_SOURCES:
        max_age = _days(setting, default)
        rows.append({"source": key, "libelle": label, "seuil_minutes": int(max_age.total_seconds() // 60), **_file_status(source_type, max_age, now)})
    worst = max(rows, key=lambda r: SEVERITE[r["etat"]])["etat"] if rows else OK
    return {"generated_at": now.isoformat(), "etat_global": worst, "sources": rows,
            "a_traiter": [r["source"] for r in rows if r["etat"] != OK]}
