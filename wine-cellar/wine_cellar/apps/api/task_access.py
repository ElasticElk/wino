from django.core.cache import cache

_TTL = 3600


def remember_task(task_id, user_id, household_id):
    cache.set(
        f"wine-task:{task_id}",
        {"user_id": user_id, "household_id": household_id},
        _TTL,
    )


def task_visible(task_id, user_id, household_id) -> bool:
    stored = cache.get(f"wine-task:{task_id}")
    if not stored:
        return False
    return (
        stored.get("user_id") == user_id
        and stored.get("household_id") == household_id
    )
