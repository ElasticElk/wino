import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0006_household_gamification_default_on"),
        ("wine", "0024_cellarachievement"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="HouseholdTasteSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("summary", models.TextField(blank=True, default="")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "household",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="taste_snapshot",
                        to="scanner.household",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="TasteIntent",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("look", "Just looking"), ("winelist", "Wine list")],
                        max_length=16,
                    ),
                ),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created", models.DateTimeField(auto_now_add=True)),
                (
                    "household",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="taste_intents",
                        to="scanner.household",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
        migrations.AddIndex(
            model_name="tasteintent",
            index=models.Index(
                fields=["household", "kind", "-created"],
                name="wine_tastei_househo_kind_idx",
            ),
        ),
    ]
