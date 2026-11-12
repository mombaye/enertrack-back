"""
Configuration du module Carburant : accès par code à 6 chiffres, puis envoi des fichiers sources (Closing initial,
mises à jour, relevés gardiennage, target, modèles de cuves) sans passer par le répertoire partagé monté.

  POST /api/fuel-tracking/configuration/acces/     {"code": "123456"} → {"token", "expires_in"}
  GET  /api/fuel-tracking/configuration/fichiers/  derniers fichiers reçus + lieu de stockage      (en-tête X-Fuel-Config-Token)
  POST /api/fuel-tracking/configuration/fichiers/  multipart file + source_type → 202 + ticket    (en-tête X-Fuel-Config-Token)
  GET  /api/fuel-tracking/configuration/fichiers/?ticket=…  état du traitement : EN_COURS, TERMINE (+ lot), ECHEC

Le traitement d'un gros fichier (Closing .xlsb de 30 Mo : ~105 s mesurées) dépasse le délai du serveur web (120 s) :
le fichier est déposé sur le volume partagé web/Celery, traité par la tâche fuel_tracking.process_config_upload
(file « interactive »), et le résultat est écrit à côté du fichier (result.json). Pas de dépendance au cache.

Sécurité :
  - le code n'est jamais stocké en clair : seule son empreinte (FUEL_CONFIG_CODE_HASH, hachage Django) est comparée ;
  - 5 essais faux par utilisateur → blocage 15 min (FUEL_CONFIG_MAX_ATTEMPTS, FUEL_CONFIG_LOCK_S) ;
  - le jeton est signé par le serveur, lié à l'utilisateur connecté et expire (FUEL_CONFIG_TOKEN_TTL_S, 2 h par défaut).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core import signing
from django.core.cache import cache
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

TOKEN_HEADER = "HTTP_X_FUEL_CONFIG_TOKEN"
_SALT = "fuel-tracking-configuration"
_CODE = re.compile(r"^\d{6}$")
STALE_S = 35 * 60  # au-delà de la limite dure de la tâche (30 min)
_TICKET = re.compile(r"^[0-9a-f]{32}$")
UPLOAD_TYPES = ("CLOSING", "SCSS", "LOLIFORCE", "TARGET_FUEL", "TERRAIN_MENSUEL", "TANK_MODELS", "HISTORICAL_REFUELING")


def _ttl() -> int:
    return int(getattr(settings, "FUEL_CONFIG_TOKEN_TTL_S", 7200))


def _max_attempts() -> int:
    return int(getattr(settings, "FUEL_CONFIG_MAX_ATTEMPTS", 5))


def _lock_s() -> int:
    return int(getattr(settings, "FUEL_CONFIG_LOCK_S", 900))


def _fail_key(user) -> str:
    return f"fuel-config:fails:{user.pk}"


def issue_token(user) -> str:
    return signing.dumps({"u": user.pk}, salt=_SALT)


def has_config_access(request) -> bool:
    token = request.META.get(TOKEN_HEADER) or ""
    if not token or not request.user or not request.user.is_authenticated:
        return False
    try:
        data = signing.loads(token, salt=_SALT, max_age=_ttl())
    except signing.BadSignature:  # inclut SignatureExpired
        return False
    return data.get("u") == request.user.pk


def pending_root() -> Path:
    """Dépôt temporaire des envois, toujours sur le volume média (monté dans web et dans les workers Celery)."""
    from fuel_tracking.services import stock_import_pipeline as P

    return P.media_root() / "_config_pending"


def ticket_dir(ticket: str) -> Path | None:
    return pending_root() / ticket if _TICKET.match(ticket or "") else None


def ticket_state(ticket: str, user) -> dict | None:
    d = ticket_dir(ticket)
    if d is None or not (d / "meta.json").is_file():
        return None
    meta = json.loads((d / "meta.json").read_text())
    if meta.get("u") != user.pk:
        return None
    res = d / "result.json"
    if not res.is_file():
        if time.time() - float(meta.get("t") or 0) > STALE_S:
            return {"ticket": ticket, "etat": "ECHEC", "file_name": meta.get("file_name"), "batch": None,
                    "detail": "Aucun résultat après 35 min : traitement interrompu. Renvoyer le fichier."}
        return {"ticket": ticket, "etat": "EN_COURS", "file_name": meta.get("file_name"), "batch": None}
    out = json.loads(res.read_text())
    out.update(ticket=ticket, file_name=meta.get("file_name"))
    if out.get("batch_id"):
        from fuel_tracking.models import FuelImportBatch
        from fuel_tracking.views_stock import _batch

        b = FuelImportBatch.objects.select_related("received_by").filter(pk=out["batch_id"]).first()
        out["batch"] = _batch(b) if b else None
    return out


def run_pending_upload(ticket: str) -> dict:
    """Traite un envoi déposé (appelé par la tâche Celery). Écrit result.json puis supprime la copie temporaire :
    le fichier reste archivé par la chaîne d'import (archive/AAAA-MM)."""
    from django.contrib.auth import get_user_model
    from fuel_tracking.services import stock_import_pipeline as P

    d = ticket_dir(ticket)
    if d is None or not (d / "meta.json").is_file():
        return {"etat": "ECHEC", "detail": "Envoi introuvable."}
    meta = json.loads((d / "meta.json").read_text())
    data_path = d / "file.bin"
    try:
        user = get_user_model().objects.filter(pk=meta.get("u")).first()
        batch = P.process_bytes(data_path.read_bytes(), meta["file_name"], meta["source_type"], origin="UPLOAD", user=user)
        result = {"etat": "TERMINE", "batch_id": batch.pk}
    except Exception as exc:  # trace complète dans les logs ; message court (sans chemin) pour l'écran
        logger.exception("[fuel_config] échec du traitement de l'envoi %s", ticket)
        result = {"etat": "ECHEC", "detail": f"Échec technique ({exc.__class__.__name__})."}
    (d / "result.json").write_text(json.dumps(result))
    data_path.unlink(missing_ok=True)
    return result


def _denied():
    return Response({"detail": "Accès Configuration requis : saisir le code à 6 chiffres.", "code": "CONFIG_CODE_REQUIRED"}, status=403)


class ConfigAccessView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser, FormParser]

    def post(self, request):
        key = _fail_key(request.user)
        fails = int(cache.get(key) or 0)
        if fails >= _max_attempts():
            return Response({"detail": f"Trop d'essais : réessayer dans {_lock_s() // 60} min.", "code": "CONFIG_LOCKED"}, status=429)
        code = str(request.data.get("code") or "").strip()
        expected = getattr(settings, "FUEL_CONFIG_CODE_HASH", "") or ""
        if not expected:
            return Response({"detail": "Code de configuration non défini sur le serveur.", "code": "CONFIG_DISABLED"}, status=503)
        if not _CODE.match(code) or not check_password(code, expected):
            fails += 1
            cache.set(key, fails, _lock_s())
            logger.warning("[fuel_config] code faux pour l'utilisateur %s (%s/%s)", request.user.pk, fails, _max_attempts())
            left = max(0, _max_attempts() - fails)
            return Response({"detail": "Code incorrect." + (f" {left} essai(s) restant(s)." if left else ""), "code": "CONFIG_BAD_CODE",
                             "essais_restants": left}, status=403)
        cache.delete(key)
        logger.info("[fuel_config] accès Configuration accordé à l'utilisateur %s", request.user.pk)
        return Response({"token": issue_token(request.user), "expires_in": _ttl()})


class ConfigFilesView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request):
        if not has_config_access(request):
            return _denied()
        ticket = request.query_params.get("ticket")
        if ticket:
            st = ticket_state(ticket, request.user)
            if st is None:
                return Response({"detail": "Envoi introuvable."}, status=404)
            return Response(st)
        from fuel_tracking.models import FuelImportBatch
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.views_stock import _batch

        qs = FuelImportBatch.objects.select_related("received_by").order_by("-received_at")[:20]
        return Response({"data": [_batch(b) for b in qs], "stockage": P.storage_label(), "types": list(UPLOAD_TYPES),
                         "formats": list(P.ACCEPTED_EXT)})

    def post(self, request):
        if not has_config_access(request):
            return _denied()
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.tasks import process_config_upload

        f = request.FILES.get("file")
        source_type = str(request.data.get("source_type") or "").upper()
        if f is None:
            return Response({"detail": "Fichier manquant."}, status=400)
        if source_type not in UPLOAD_TYPES:
            return Response({"detail": f"Type de fichier invalide ({', '.join(UPLOAD_TYPES)})."}, status=400)
        if not f.name.lower().endswith(P.ACCEPTED_EXT):
            return Response({"detail": "Format accepté : .xlsx, .xlsm, .xlsb."}, status=400)
        max_mb = int(getattr(settings, "FUEL_CONFIG_MAX_UPLOAD_MB", 50))
        if f.size and f.size > max_mb * 1024 * 1024:
            return Response({"detail": f"Fichier trop volumineux (maximum {max_mb} Mo)."}, status=400)
        ticket = uuid.uuid4().hex
        d = pending_root() / ticket
        try:
            d.mkdir(parents=True, exist_ok=False)
            with open(d / "file.bin", "wb") as out:
                for chunk in f.chunks():
                    out.write(chunk)
            (d / "meta.json").write_text(json.dumps({"u": request.user.pk, "file_name": Path(f.name).name[:255],
                                                     "source_type": source_type, "t": time.time()}))
        except OSError as e:
            logger.exception("[fuel_config] stockage indisponible")
            shutil.rmtree(d, ignore_errors=True)
            return Response({"detail": f"Stockage des fichiers indisponible ({e.__class__.__name__})."}, status=503)
        try:
            process_config_upload.apply_async(args=[ticket], queue="interactive")
        except Exception as e:  # broker indisponible : on ne laisse pas un envoi orphelin
            logger.exception("[fuel_config] file de traitement indisponible")
            shutil.rmtree(d, ignore_errors=True)
            return Response({"detail": f"File de traitement indisponible ({e.__class__.__name__}) : réessayer plus tard."}, status=503)
        logger.info("[fuel_config] envoi %s reçu (%s, %s octets) par l'utilisateur %s", ticket, source_type, f.size, request.user.pk)
        return Response({"ticket": ticket, "etat": "EN_COURS", "file_name": Path(f.name).name[:255], "batch": None}, status=202)
