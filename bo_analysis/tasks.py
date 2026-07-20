# bo_analysis/tasks.py
# Notifications asynchrones du workflow BO (activation → alerte BO → soumission → alerte user).
#
# L'envoi d'email ne doit jamais bloquer/faire échouer le workflow : EMAIL_BACKEND retombe sur
# la console tant qu'aucun SMTP réel n'est configuré (voir settings.py), et l'appel est entouré
# d'un try/except loggé en plus de fail_silently=True.

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _send_email_safe(to_email: str | None, subject: str, body: str, context_label: str) -> bool:
    if not to_email:
        return False
    try:
        send_mail(subject, body, getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@enertrack.local"),
                   [to_email], fail_silently=True)
        return True
    except Exception as e:
        logger.warning("[bo_analysis] Email non envoyé (%s): %s", context_label, e)
        return False


@shared_task(bind=True, max_retries=2, default_retry_delay=30, name="bo_analysis.notify_bo_request_created")
def notify_bo_request_created(self, request_id: int):
    from core.models import Notification
    from users.models import CustomUser

    from .models import BOAnalysisRequest

    req = BOAnalysisRequest.objects.prefetch_related("targeted_bos").select_related("site").get(id=request_id)
    targeted = list(req.targeted_bos.all())
    recipients = targeted if targeted else list(
        CustomUser.objects.filter(role="bo", is_active=True)
    )

    message = f"Nouvelle demande d'analyse BO — site {req.site.site_id} ({req.month:02d}/{req.year})"

    for user in recipients:
        notif = Notification.objects.create(
            recipient=user, verb="bo_request_created", message=message,
            related_app="bo_analysis", related_model="BOAnalysisRequest", related_id=req.id,
            site_id_ref=req.site.site_id, year=req.year, month=req.month,
        )
        sent = _send_email_safe(user.email, "Nouvelle demande d'analyse BO", message, f"request #{req.id}")
        if sent:
            notif.email_sent = True
            notif.save(update_fields=["email_sent"])


@shared_task(bind=True, max_retries=2, default_retry_delay=30, name="bo_analysis.notify_bo_bulk_request_created")
def notify_bo_bulk_request_created(self, request_ids: list[int]):
    """
    Notifie UNE seule fois par BO destinataire pour un lot de demandes créées
    en masse (ex: activation sur tous les sites NOK d'une période) — évite de
    spammer la cloche/l'email d'un BO avec une notification par site.
    """
    from core.models import Notification
    from users.models import CustomUser

    from .models import BOAnalysisRequest

    if not request_ids:
        return

    reqs = list(
        BOAnalysisRequest.objects.prefetch_related("targeted_bos").select_related("site")
        .filter(id__in=request_ids)
    )
    if not reqs:
        return

    first = reqs[0]
    targeted = list(first.targeted_bos.all())
    recipients = targeted if targeted else list(
        CustomUser.objects.filter(role="bo", is_active=True)
    )

    count = len(reqs)
    period = f"{first.month:02d}/{first.year}"
    message = f"{count} nouvelle(s) demande(s) d'analyse BO — {period}"

    for user in recipients:
        notif = Notification.objects.create(
            recipient=user, verb="bo_bulk_request_created", message=message,
            related_app="bo_analysis", related_model="BOAnalysisRequest",
            year=first.year, month=first.month,
        )
        sent = _send_email_safe(user.email, "Nouvelles demandes d'analyse BO", message, f"bulk #{request_ids[0]}")
        if sent:
            notif.email_sent = True
            notif.save(update_fields=["email_sent"])


@shared_task(bind=True, max_retries=2, default_retry_delay=30, name="bo_analysis.notify_bo_analysis_submitted")
def notify_bo_analysis_submitted(self, request_id: int):
    from core.models import Notification

    from .models import BOAnalysisRequest

    req = BOAnalysisRequest.objects.select_related("site", "requested_by").get(id=request_id)
    message = f"Analyse BO complétée — site {req.site.site_id} ({req.month:02d}/{req.year})"

    notif = Notification.objects.create(
        recipient=req.requested_by, verb="bo_analysis_submitted", message=message,
        related_app="bo_analysis", related_model="BOAnalysisRequest", related_id=req.id,
        site_id_ref=req.site.site_id, year=req.year, month=req.month,
    )
    sent = _send_email_safe(req.requested_by.email, "Analyse BO complétée", message, f"request #{req.id}")
    if sent:
        notif.email_sent = True
        notif.save(update_fields=["email_sent"])
