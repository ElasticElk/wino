from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0005_household_gamification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="household",
            name="gamification_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
