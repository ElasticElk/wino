from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.views.generic import DetailView, FormView, ListView

from wine_cellar.apps.club.forms import ClubSettingsForm
from wine_cellar.apps.club.models import Club, ClubEvent, ClubMembership
from wine_cellar.apps.club.services import apply_policy, membership_of
from wine_cellar.apps.scanner.flags import flag_on


class ClubWebMixin:
    def dispatch(self, request, *args, **kwargs):
        if not flag_on("feature_clubs"):
            raise Http404("not_enabled")
        return super().dispatch(request, *args, **kwargs)


class ClubListView(ClubWebMixin, ListView):
    template_name = "club_list.html"
    context_object_name = "memberships"

    def get_queryset(self):
        return (
            ClubMembership.objects.filter(user=self.request.user)
            .select_related("club")
            .order_by("-is_last_used", "club__name")
        )


class ClubDetailView(ClubWebMixin, DetailView):
    template_name = "club_detail.html"
    context_object_name = "club"
    model = Club

    def get_object(self, queryset=None):
        club = super().get_object(queryset)
        membership = membership_of(self.request.user, club.id)
        if membership is None:
            raise Http404("not_found")
        self.membership = membership
        return club

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["membership"] = self.membership
        context["events"] = (
            ClubEvent.objects.filter(club=self.object)
            .select_related("actor")
            .order_by("-created")[:20]
        )
        return context


class ClubSettingsView(ClubWebMixin, FormView):
    template_name = "club_settings.html"
    form_class = ClubSettingsForm

    def dispatch(self, request, *args, **kwargs):
        if not flag_on("feature_clubs"):
            raise Http404("not_enabled")
        self.membership = membership_of(request.user, kwargs.get("pk"))
        if self.membership is None:
            raise Http404("not_found")
        if self.membership.role != ClubMembership.ROLE_OWNER:
            raise Http404("not_found")
        self.club = self.membership.club
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            "cellar_visibility": self.club.cellar_visibility,
            "badges_visible": self.club.badges_visible,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["club"] = self.club
        return context

    def form_valid(self, form):
        change, club = apply_policy(
            self.club,
            form.cleaned_data["cellar_visibility"],
            form.cleaned_data.get("badges_visible"),
        )
        if change == "widen":
            from django.db import transaction

            from wine_cellar.apps.club.tasks import club_widen_notice

            transaction.on_commit(lambda cid=club.id: club_widen_notice.delay(cid))
        return redirect(reverse("club-detail", kwargs={"pk": self.club.pk}))
