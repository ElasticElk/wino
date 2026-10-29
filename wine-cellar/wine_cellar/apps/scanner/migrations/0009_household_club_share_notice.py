from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0008_household_nudge_and_last_seen"),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="club_share_notice_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
