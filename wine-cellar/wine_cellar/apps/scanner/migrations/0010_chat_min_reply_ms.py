from django.db import migrations


def seed_chat_min_reply_ms(apps, schema_editor):
    ServiceConfig = apps.get_model("scanner", "ServiceConfig")
    ServiceConfig.objects.get_or_create(
        key="chat_min_reply_ms",
        defaults={
            "value": "1000",
            "description": "Minimum milliseconds from a typed Telegram message to the first reply (0 disables).",
        },
    )


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0009_household_club_share_notice"),
    ]

    operations = [
        migrations.RunPython(seed_chat_min_reply_ms, migrations.RunPython.noop),
    ]
