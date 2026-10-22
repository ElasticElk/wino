from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wine", "0028_wine_nzd_currency"),
    ]

    operations = [
        migrations.AlterField(
            model_name="tasteintent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("look", "Just looking"),
                    ("winelist", "Wine list"),
                    ("ask", "Asked for a recommendation"),
                ],
                max_length=16,
            ),
        ),
    ]
