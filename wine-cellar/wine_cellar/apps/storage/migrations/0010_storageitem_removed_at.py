from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("storage", "0009_storageitem_wine_deleted_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="storageitem",
            name="removed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
