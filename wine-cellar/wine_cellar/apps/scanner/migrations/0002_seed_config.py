from django.db import migrations


def seed_config(apps, schema_editor):
    ServiceConfig = apps.get_model("scanner", "ServiceConfig")
    defaults = [
        ("bot_token", "", "Telegram bot token from BotFather"),
        ("grok_api_key", "", "Grok/xAI API key for vision"),
        ("web_url", "", "Public URL for Telegram links (e.g. http://nas.local:8085)"),
        ("default_storage", "Wine Fridge", "Default storage name for new stock"),
    ]
    for key, value, description in defaults:
        ServiceConfig.objects.get_or_create(
            key=key, defaults={"value": value, "description": description}
        )


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_config, migrations.RunPython.noop),
    ]
