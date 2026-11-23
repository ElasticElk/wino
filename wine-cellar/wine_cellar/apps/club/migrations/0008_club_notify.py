import django.db.models.deletion
from django.db import migrations, models


def feed_muted_to_mode(apps, schema_editor):
    Membership = apps.get_model("club", "ClubMembership")
    Membership.objects.filter(feed_muted=True).update(notify_mode="muted")


def drop_eventless_rows(apps, schema_editor):
    """Reverse: event goes back to NOT NULL, so rows without one must go."""
    apps.get_model("club", "ClubNotice").objects.filter(event__isnull=True).delete()
    apps.get_model("club", "ClubEventDelivery").objects.filter(
        event__isnull=True,
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0007_clubevent_kind_ask"),
    ]

    operations = [
        migrations.AddField(
            model_name="club",
            name="notify_crossing_sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="clubmembership",
            name="notify_mode",
            field=models.CharField(
                blank=True,
                choices=[
                    ("instant", "Every update"),
                    ("digest", "Daily recap"),
                    ("muted", "Muted"),
                ],
                default=None,
                max_length=8,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="clubnotice",
            name="hold",
            field=models.CharField(
                blank=True,
                choices=[("quiet", "Quiet hours"), ("recap", "Daily recap")],
                default="",
                max_length=6,
            ),
        ),
        migrations.AlterField(
            model_name="clubnotice",
            name="event",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="notices",
                to="club.clubevent",
            ),
        ),
        migrations.AlterField(
            model_name="clubeventdelivery",
            name="event",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="deliveries",
                to="club.clubevent",
            ),
        ),
        migrations.AlterField(
            model_name="clubeventdelivery",
            name="kind",
            field=models.CharField(
                choices=[
                    ("card", "Card"),
                    ("ping", "Ping"),
                    ("ask", "Ask"),
                    ("summary", "Summary"),
                ],
                default="card",
                max_length=8,
            ),
        ),
        # Listed after the nullable AlterFields so that, reversed, it runs
        # before they turn NOT NULL again.
        migrations.RunPython(migrations.RunPython.noop, drop_eventless_rows),
        migrations.AlterField(
            model_name="clubnotice",
            name="kind",
            field=models.CharField(
                choices=[
                    ("comment", "Comment"),
                    ("reaction", "Reaction"),
                    ("card", "Feed card"),
                    ("ask", "Ask"),
                    ("tasteoff", "Taste-off"),
                    ("crossing", "Recap switch"),
                ],
                default="comment",
                max_length=12,
            ),
        ),
        migrations.RunPython(feed_muted_to_mode, migrations.RunPython.noop),
    ]
