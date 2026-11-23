from django.db import migrations, models


def merge_duplicates(apps, schema_editor):
    """Fold duplicate unsent notices (same recipient and event) into one."""
    ClubNotice = apps.get_model("club", "ClubNotice")
    seen = {}
    for notice in ClubNotice.objects.filter(sent_at__isnull=True).order_by("id"):
        key = (notice.recipient_id, notice.event_id, notice.kind)
        keep = seen.get(key)
        if keep is None:
            seen[key] = notice
            continue
        ids = list((keep.payload or {}).get("comment_ids") or [])
        for cid in (notice.payload or {}).get("comment_ids") or []:
            if cid not in ids:
                ids.append(cid)
        keep.payload = {"comment_ids": ids}
        keep.due_at = min(keep.due_at, notice.due_at)
        keep.save(update_fields=["payload", "due_at"])
        notice.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0004_club_threads"),
    ]

    operations = [
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="clubnotice",
            constraint=models.UniqueConstraint(
                condition=models.Q(("sent_at__isnull", True)),
                fields=("recipient", "event", "kind"),
                name="clubnotice_one_pending",
            ),
        ),
    ]
