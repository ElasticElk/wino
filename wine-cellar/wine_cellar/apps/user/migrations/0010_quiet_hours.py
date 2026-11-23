import datetime

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0009_alter_usersettings_language"),
    ]

    operations = [
        migrations.AddField(
            model_name="usersettings",
            name="quiet_tz",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="usersettings",
            name="quiet_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="usersettings",
            name="quiet_start",
            field=models.TimeField(default=datetime.time(22, 0)),
        ),
        migrations.AddField(
            model_name="usersettings",
            name="quiet_end",
            field=models.TimeField(default=datetime.time(8, 0)),
        ),
        migrations.AddField(
            model_name="usersettings",
            name="last_summary_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
