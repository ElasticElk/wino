from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("user", "0007_usersettings_nzd_currency"),
    ]

    operations = [
        migrations.AddField(
            model_name="usersettings",
            name="country_prompted",
            field=models.BooleanField(default=False),
        ),
    ]
