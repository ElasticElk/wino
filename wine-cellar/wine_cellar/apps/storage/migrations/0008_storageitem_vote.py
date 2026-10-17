from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("storage", "0007_rename_default_shelf"),
    ]

    operations = [
        migrations.AddField(
            model_name="storageitem",
            name="vote",
            field=models.CharField(
                blank=True,
                choices=[
                    ("down", "Thumbs down"),
                    ("meh", "Indifferent"),
                    ("up", "Thumbs up"),
                ],
                max_length=8,
                null=True,
            ),
        ),
    ]
