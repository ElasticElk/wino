from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0007_householdinvite"),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="empty_nudge_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="telegramuser",
            name="last_seen_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
