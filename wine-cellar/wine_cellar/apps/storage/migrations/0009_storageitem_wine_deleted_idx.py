from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("storage", "0008_storageitem_vote"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="storageitem",
            index=models.Index(
                fields=["wine", "deleted"],
                name="storageitem_wine_deleted_idx",
            ),
        ),
    ]
