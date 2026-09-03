"""Give every storage a household.

Access control moved from `user` to `household` across the web layer. Storages
created before that (auto-created "Default Shelf" placeholders) have a NULL
household, which makes them invisible to the API, the bot and now the web UI
alike. Map each to its creator's household.

Reversible as a no-op: unsetting household again would only recreate orphans.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Storage = apps.get_model("storage", "Storage")
    HouseholdMembership = apps.get_model("scanner", "HouseholdMembership")

    for storage in Storage.objects.filter(household__isnull=True).select_related("user"):
        if storage.user_id is None:
            continue
        membership = (
            HouseholdMembership.objects.filter(user_id=storage.user_id, is_default=True)
            .first()
            or HouseholdMembership.objects.filter(user_id=storage.user_id).first()
        )
        if membership is None:
            # No household to attach it to; leave it alone rather than guess.
            continue
        storage.household_id = membership.household_id
        storage.save(update_fields=["household"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("storage", "0005_storage_household"),
        # Must be 0004, not 0001: HouseholdMembership is created in 0003 and
        # populated in 0004. Depending on 0001 works against an already-migrated
        # database but explodes building a fresh one, because the historical
        # model state at that node has no HouseholdMembership.
        ("scanner", "0004_populate_households"),
    ]

    operations = [migrations.RunPython(backfill, noop)]
