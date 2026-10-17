from django.conf import settings
from django.urls import reverse_lazy
from django.utils import translation
from django.views.generic import UpdateView

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.scanner.models import HouseholdMembership
from wine_cellar.apps.user.forms import UserSettingsForm
from wine_cellar.apps.user.models import UserSettings


class UserSettingsView(UpdateView):
    template_name = "settings.html"
    form_class = UserSettingsForm
    success_url = reverse_lazy("user-settings")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        household = get_user_household(self.request.user)
        membership = None
        if household:
            membership = HouseholdMembership.objects.filter(
                user=self.request.user, household=household,
            ).first()
        context["household"] = household
        context["can_toggle_gamification"] = bool(
            membership and membership.role == "owner"
        )
        context["gamification_enabled"] = bool(
            household and household.gamification_enabled
        )
        return context

    def form_valid(self, form):
        response = super().form_valid(form)
        user_language = form.cleaned_data["language"]
        translation.activate(user_language)
        response.set_cookie(settings.LANGUAGE_COOKIE_NAME, user_language)
        if self.request.POST.get("gamification") is not None:
            household = get_user_household(self.request.user)
            membership = HouseholdMembership.objects.filter(
                user=self.request.user, household=household,
            ).first() if household else None
            if membership and membership.role == "owner":
                enabled = self.request.POST.get("gamification") == "on"
                household.gamification_enabled = enabled
                household.save(update_fields=["gamification_enabled"])
                if enabled:
                    from wine_cellar.apps.wine.achievements import backfill
                    backfill(household, self.request.user)
        return response

    def get_object(self, queryset=None):
        user = self.request.user
        return get_user_settings(user)


def get_user_settings(user):
    if not hasattr(user, "user_settings"):
        user.user_settings = UserSettings()
    return user.user_settings
