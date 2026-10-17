import django.db.models.deletion
import wine_cellar.apps.scanner.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0006_household_gamification_default_on"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="HouseholdInvite",
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
                    "code",
                    models.CharField(
                        default=wine_cellar.apps.scanner.models.generate_invite_code,
                        max_length=8,
                        unique=True,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("join", "Join existing"), ("new", "New cellar")],
                        max_length=8,
                    ),
                ),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="household_invites",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "household",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invites",
                        to="scanner.household",
                    ),
                ),
                (
                    "used_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="redeemed_invites",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created"],
            },
        ),
    ]
