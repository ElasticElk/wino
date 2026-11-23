from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0008_club_notify"),
    ]

    operations = [
        migrations.AddField(
            model_name="clubevent",
            name="note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="clubevent",
            name="note_added_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
