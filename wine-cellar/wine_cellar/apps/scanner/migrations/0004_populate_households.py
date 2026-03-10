"""Data migration: create a default Household for each existing user and
populate household FKs on Wine, Storage, and Scanner."""

from django.db import migrations


def populate_households(apps, schema_editor):
    User = apps.get_model("auth", "User")
    Household = apps.get_model("scanner", "Household")
    HouseholdMembership = apps.get_model("scanner", "HouseholdMembership")
    Wine = apps.get_model("wine", "Wine")
    Storage = apps.get_model("storage", "Storage")
    Scanner = apps.get_model("scanner", "Scanner")

    for user in User.objects.all():
        # Create a household for each existing user
        household = Household.objects.create(name=f"{user.username}'s Home")
        HouseholdMembership.objects.create(
            household=household, user=user, role="owner", is_default=True,
        )

        # Assign all their existing data to this household
        Wine.objects.filter(user=user).update(household=household)
        Storage.objects.filter(user=user).update(household=household)
        Scanner.objects.filter(user=user).update(household=household)


class Migration(migrations.Migration):

    dependencies = [
        ("scanner", "0003_household_scanner_household_householdmembership"),
        ("wine", "0020_remove_wine_unique_wine_wine_household_and_more"),
        ("storage", "0005_storage_household"),
    ]

    operations = [
        migrations.RunPython(populate_households, migrations.RunPython.noop),
    ]
