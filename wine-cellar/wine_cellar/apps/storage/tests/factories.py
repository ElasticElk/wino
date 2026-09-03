import random

import factory
from factory.django import DjangoModelFactory

from wine_cellar.apps.storage.models import Storage, StorageItem
from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.tests.factories import WineFactory


class StorageFactory(DjangoModelFactory):
    class Meta:
        model = Storage

    name = factory.Faker("name")
    location = factory.Faker("address")
    rows = random.randint(1, 10)
    columns = random.randint(1, 10)
    user = factory.SubFactory(UserFactory)
    household = factory.LazyAttribute(lambda o: get_user_household(o.user))


class StorageItemFactory(DjangoModelFactory):
    class Meta:
        model = StorageItem

    storage = factory.SubFactory(StorageFactory)
    wine = factory.SubFactory(WineFactory)
