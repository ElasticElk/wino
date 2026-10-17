from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wine", "0025_household_taste"),
    ]

    operations = [
        migrations.AddField(
            model_name="wine",
            name="drink_by_reminded_on",
            field=models.DateField(blank=True, null=True),
        ),
    ]
