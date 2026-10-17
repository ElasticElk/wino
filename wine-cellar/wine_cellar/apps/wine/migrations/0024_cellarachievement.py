import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0005_household_gamification"),
        ("wine", "0023_split_producer_from_comment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CellarAchievement",
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
                ("slug", models.CharField(max_length=64)),
                ("subject", models.CharField(blank=True, default="", max_length=120)),
                ("emoji", models.CharField(default="🍷", max_length=8)),
                ("title", models.CharField(max_length=120)),
                ("detail", models.CharField(blank=True, max_length=240)),
                ("unlocked_at", models.DateTimeField(auto_now_add=True)),
                (
                    "household",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="achievements",
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
                "ordering": ["-unlocked_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="cellarachievement",
            constraint=models.UniqueConstraint(
                fields=("household", "slug", "subject"),
                name="unique_achievement_per_household",
            ),
        ),
    ]
