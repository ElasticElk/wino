"""Household resolution shared by the web and API layers.

Access control is by **household**, not by user: everyone in a household sees
all of its wine, storages and stock. The `user` FK on Wine / Storage /
StorageItem is retained as provenance — who added the row — and must not be
used for filtering. Before this, the web layer filtered on `user` while the API
and bot filtered on `household`, so the same person saw two different cellars
depending on which one they opened.
"""

from wine_cellar.apps.scanner.models import HouseholdMembership


def get_user_household(user, household_id=None):
    """Return the user's active household, or None if they belong to none.

    Priority: an explicitly requested household the user actually belongs to,
    then their default membership, then their first. A requested household the
    user is not a member of is ignored rather than honoured.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    if household_id is not None:
        membership = (
            HouseholdMembership.objects.filter(user=user, household_id=household_id)
            .select_related("household")
            .first()
        )
        if membership:
            return membership.household

    membership = (
        HouseholdMembership.objects.filter(user=user, is_default=True)
        .select_related("household")
        .first()
        or HouseholdMembership.objects.filter(user=user)
        .select_related("household")
        .first()
    )
    return membership.household if membership else None


def scope(queryset, household, field="household"):
    """Filter a queryset to one household, or to nothing when there isn't one.

    The empty case matters: `filter(household=None)` would match every row whose
    household is NULL, so a user with no membership would be shown orphaned
    data. Returning none() makes "no household" mean "no access".
    """
    if household is None:
        return queryset.none()
    return queryset.filter(**{field: household})


class HouseholdScopedMixin:
    """Adds `self.household` to a class-based view, resolved once per request."""

    _household_cache = None

    @property
    def household(self):
        if self._household_cache is None:
            self._household_cache = get_user_household(self.request.user)
        return self._household_cache

    def scoped(self, queryset, field="household"):
        return scope(queryset, self.household, field=field)


def household_member_ids(user):
    """User ids sharing a household with `user`, including their own.

    Lookup tables (Grape, Region, Size, ...) carry a `user` FK rather than a
    household, so shared vocabulary is expressed as "anyone in my household".
    Falls back to just this user when they belong to no household.
    """
    household = get_user_household(user)
    if household is None:
        return [user.pk] if user is not None and getattr(user, "pk", None) else []
    return list(
        HouseholdMembership.objects.filter(household=household).values_list(
            "user_id", flat=True,
        )
    )
