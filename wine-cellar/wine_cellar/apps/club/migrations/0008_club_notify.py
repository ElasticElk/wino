import django.db.models.deletion
from django.db import migrations, models


def feed_muted_to_mode(apps, schema_editor):
    Membership = apps.get_model("club", "ClubMembership")
    Membership.objects.filter(feed_muted=True).update(notify_mode="muted")


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
            model_name="clubnotice",
            name="kind",
            field=models.CharField(
                choices=[
                    ("comment", "Comment"),
                    ("reaction", "Reaction"),
                    ("card", "Feed card"),
                    ("ask", "Ask"),
                    ("tasteoff", "Taste-off"),
                ],
                default="comment",
                max_length=12,
            ),
        ),
        migrations.RunPython(feed_muted_to_mode, migrations.RunPython.noop),
    ]
