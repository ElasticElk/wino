"""Queue club feed events after stock mutations. No-ops if Celery is down."""

from __future__ import annotations

from django.db import transaction


def queue_club_event(item_id, actor_id, kind: str) -> None:
    """Fan out after commit so gunicorn never waits on Telegram."""
    if not item_id or not actor_id or not kind:
        return

    def _go(iid=int(item_id), aid=int(actor_id), k=str(kind)):
        from wine_cellar.apps.club.tasks import publish_club_event

        publish_club_event.delay(iid, aid, k)

    transaction.on_commit(_go)
