from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wine", "0026_wine_drink_by_reminded_on"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="wine",
            index=models.Index(
                fields=["household", "drink_by"],
                name="wine_household_drink_by_idx",
            ),
        ),
    ]
