# users/emails.py
# Emails transactionnels liés au cycle de vie d'un compte (création, désactivation).
#
# Envoyés de façon synchrone (pas de tâche Celery) : le mot de passe temporaire ne
# doit jamais transiter par la file Celery/Redis, même brièvement. L'envoi ne doit
# en revanche jamais faire échouer l'action admin (création/toggle) — voir
# fail_silently=True + try/except, même principe que bo_analysis/tasks.py.

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def _send_email_safe(to_email: str | None, subject: str, body: str, context_label: str) -> bool:
    if not to_email:
        return False
    try:
        send_mail(
            subject, body,
            getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@enertrack.local"),
            [to_email], fail_silently=True,
        )
        return True
    except Exception as e:
        logger.warning("[users] Email non envoyé (%s): %s", context_label, e)
        return False


def send_account_created_email(user, password: str) -> bool:
    display_name = user.first_name or user.username
    frontend_url = getattr(settings, "FRONTEND_URL", "https://egrid.camusatsn.com")

    subject = "Votre compte EnerTrack a été créé"
    body = (
        f"Bonjour {display_name},\n\n"
        f"Un compte EnerTrack a été créé pour vous.\n\n"
        f"Identifiant : {user.username}\n"
        f"Mot de passe temporaire : {password}\n\n"
        f"Connectez-vous sur {frontend_url} et pensez à changer votre mot de passe "
        f"dès que possible.\n\n"
        f"— EnerTrack"
    )
    return _send_email_safe(user.email, subject, body, f"création compte #{user.pk}")


def send_account_deactivated_email(user) -> bool:
    display_name = user.first_name or user.username

    subject = "Votre compte EnerTrack a été désactivé"
    body = (
        f"Bonjour {display_name},\n\n"
        f"Votre compte EnerTrack ({user.username}) vient d'être désactivé. "
        f"Vous ne pouvez plus vous connecter à la plateforme.\n\n"
        f"Si vous pensez qu'il s'agit d'une erreur, contactez votre administrateur.\n\n"
        f"— EnerTrack"
    )
    return _send_email_safe(user.email, subject, body, f"désactivation compte #{user.pk}")
