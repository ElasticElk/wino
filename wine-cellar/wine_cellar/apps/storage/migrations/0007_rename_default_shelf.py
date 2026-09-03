"""Rename the auto-created "Default Shelf" placeholders to "Unsorted".

These were created one-per-user by an older code path and were invisible while
the web layer scoped by user. Household scoping surfaced them, so give them a
name that says what they are for: a landing spot for bottles whose location
isn't known yet.

Only touches placeholders that still hold no bottles — one that has been put to
use has been adopted deliberately and keeps its name.
"""

from django.db import migrations

OLD_NAME = "Default Shelf"
NEW_NAME = "Unsorted"


def rename(apps, schema_editor):
    Storage = apps.get_model("storage", "Storage")
    StorageItem = apps.get_model("storage", "StorageItem")

    for storage in Storage.objects.filter(name=OLD_NAME):
        in_use = StorageItem.objects.filter(storage=storage, deleted=False).exists()
        if in_use:
            continue
        storage.name = NEW_NAME
        storage.save(update_fields=["name"])


def unrename(apps, schema_editor):
    Storage = apps.get_model("storage", "Storage")
    Storage.objects.filter(name=NEW_NAME).update(name=OLD_NAME)


class Migration(migrations.Migration):
    dependencies = [("storage", "0006_backfill_storage_household")]

    operations = [migrations.RunPython(rename, unrename)]
