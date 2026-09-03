"""Move the producer out of `comment` and into its own field.

Producer used to be packed into the comment as "Producer: X | tasting notes"
and parsed back by prefix match in four separate places. Beyond the fragility,
it lost data: WineUpdateView rebuilt the whole string from producer +
description, so a PATCH carrying a producer but no description — exactly what
the "Update from label" flow sends when Grok reads a producer but writes no
notes — replaced the tasting notes with "Producer: X".

Splits on the first " | " only, so a pipe inside the notes survives.
"""

from django.db import migrations

PREFIX = "Producer: "


def split_out(apps, schema_editor):
    Wine = apps.get_model("wine", "Wine")
    for wine in Wine.objects.exclude(comment="").exclude(comment__isnull=True):
        comment = wine.comment
        if not comment.startswith(PREFIX):
            continue
        producer, separator, notes = comment[len(PREFIX):].partition(" | ")
        wine.producer = producer.strip()[:100]
        wine.comment = notes.strip() if separator else ""
        wine.save(update_fields=["producer", "comment"])


def recombine(apps, schema_editor):
    """Put it back, so the migration is genuinely reversible."""
    Wine = apps.get_model("wine", "Wine")
    for wine in Wine.objects.exclude(producer=""):
        parts = [f"{PREFIX}{wine.producer}"]
        if wine.comment:
            parts.append(wine.comment)
        wine.comment = " | ".join(parts)[:500]
        wine.producer = ""
        wine.save(update_fields=["producer", "comment"])


class Migration(migrations.Migration):
    dependencies = [("wine", "0022_wine_producer")]

    operations = [migrations.RunPython(split_out, recombine)]
