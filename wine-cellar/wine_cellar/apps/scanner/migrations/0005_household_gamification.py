from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0004_populate_households"),
    ]

    operations = [
        migrations.AddField(
            model_name="household",
            name="gamification_enabled",
            field=models.BooleanField(default=False),
        ),
    ]
