from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("club", "0006_club_reactions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="clubevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("opened", "Opened"),
                    ("voted", "Voted"),
                    ("retracted", "Retracted"),
                    ("joined", "Joined"),
                    ("left", "Left"),
                    ("tasteoff", "Taste-off"),
                    ("comment", "Comment"),
                    ("staff_note", "Staff note"),
                    ("ask", "Ask the club"),
                ],
                max_length=12,
            ),
        ),
    ]
