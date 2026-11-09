"""Envoi des tâches Celery déclenchées par un utilisateur (import facturation, certification).

Problème corrigé : un import restait « En file d'attente… » sans fin quand aucun worker
n'écoutait la file visée (service celery-interactive absent ou arrêté, worker en redémarrage).
Ici on vérifie d'abord qui écoute : file préférée, sinon file par défaut, sinon erreur explicite.
"""
import logging

from enertrack_backend.celery import app

logger = logging.getLogger(__name__)

INSPECT_TIMEOUT_S = 1.5


class NoWorkerAvailable(RuntimeError):
    """Aucun worker Celery ne consomme de file capable de traiter la tâche."""


def consumed_queues(timeout: float = INSPECT_TIMEOUT_S):
    """Noms des files écoutées par au moins un worker vivant ; None si l'inspection est impossible."""
    try:
        replies = app.control.inspect(timeout=timeout).active_queues() or {}
    except Exception as exc:  # broker injoignable, etc. — on ne bloque pas l'envoi pour autant
        logger.warning("[dispatch] inspection des workers impossible : %s", exc)
        return None
    return {q.get("name") for queues in replies.values() for q in (queues or [])}


def pick_queue(preferred, fallbacks=("celery",), queues=None):
    """File à utiliser : la préférée si écoutée, sinon le premier repli écouté.

    Lève NoWorkerAvailable si aucun worker n'écoute ni la préférée ni un repli.
    Retourne None si l'état des workers est inconnu (on laisse alors le routage par défaut).
    """
    if queues is None:
        queues = consumed_queues()
    if queues is None:
        return None
    for name in (preferred, *fallbacks):
        if name in queues:
            if name != preferred:
                logger.warning("[dispatch] aucun worker sur la file %r — repli sur %r", preferred, name)
            return name
    raise NoWorkerAvailable(
        f"Aucun worker Celery n'écoute la file {preferred!r} ni {', '.join(fallbacks)} "
        f"(files écoutées : {sorted(q for q in queues if q) or 'aucune'})."
    )


def enqueue(task, args=(), preferred="interactive", fallbacks=("celery",)):
    """apply_async sur une file réellement écoutée (voir pick_queue)."""
    queue = pick_queue(preferred, fallbacks)
    if queue is None:
        return task.apply_async(args=args)
    return task.apply_async(args=args, queue=queue)
