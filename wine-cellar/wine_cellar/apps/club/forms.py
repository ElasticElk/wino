from django import forms

from wine_cellar.apps.club.models import CELLAR_VISIBILITY


class ClubSettingsForm(forms.Form):
    cellar_visibility = forms.ChoiceField(
        choices=CELLAR_VISIBILITY,
        widget=forms.RadioSelect,
        initial="full",
    )
    badges_visible = forms.BooleanField(required=False, initial=True)
