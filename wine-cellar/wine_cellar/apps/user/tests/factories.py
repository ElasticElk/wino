import factory
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from factory import post_generation
from factory.django import DjangoModelFactory


class UserFactory(DjangoModelFactory):
    class Meta:
        model = get_user_model()

    username = factory.Sequence(lambda n: "user%d" % n)
    email = factory.Sequence(lambda n: "user%d@wine-cellar.net" % n)
    password = make_password("password")

    @post_generation
    def household(obj, create, extracted, **kwargs):
        """Every user belongs to a household.

        Access control is by household, so a user without one can see nothing —
        which is right in production (they always have one) but would make every
        fixture invisible here. Pass `household=` to put two users in the same
        one; otherwise each gets their own, preserving isolation tests.
        """
        if not create:
            return
        from wine_cellar.apps.scanner.models import Household, HouseholdMembership

        household = extracted or Household.objects.create(
            name=f"{obj.username}-household",
        )
        HouseholdMembership.objects.create(
            user=obj, household=household, role="owner",
        )
