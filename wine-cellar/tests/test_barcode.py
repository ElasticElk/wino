import pytest
from django.urls import reverse
from rest_framework.authtoken.models import Token

from wine_cellar.apps.scanner.households import get_user_household
from wine_cellar.apps.user.tests.factories import UserFactory
from wine_cellar.apps.wine.barcodes import accepted_barcode, gs1_check_ok, normalise_barcode
from wine_cellar.apps.wine.models import Wine
from wine_cellar.apps.wine.tests.factories import WineFactory


EAN13 = "0123456789012"
UPC_A = "123456789012"
GTIN14 = "00123456789012"


def test_normalise_upc_a_and_gtin14_to_ean13():
    assert normalise_barcode(UPC_A) == EAN13
    assert normalise_barcode(GTIN14) == EAN13
    assert normalise_barcode(EAN13) == EAN13
    assert normalise_barcode("841-0591-003359") == "8410591003359"


def test_normalise_leaves_invalid_12_digit_code_unchanged():
    # Wine #9's stored scan: 12 digits, UPC-A check digit should be 2 not 4.
    assert normalise_barcode("506558704244") == "506558704244"


def test_accepted_barcode_requires_gs1_check_digit():
    assert gs1_check_ok("8026856001468")
    assert accepted_barcode("8026856001468") == "8026856001468"
    assert accepted_barcode("8026856001469") is None
    assert accepted_barcode("506558704244") is None


@pytest.fixture
def cellar(db):
    user = UserFactory()
    household = get_user_household(user)
    wine = WineFactory(
        user=user, household=household, name="Viña Pomal", barcode=EAN13,
    )
    token, _ = Token.objects.get_or_create(user=user)
    return user, household, wine, token


def _auth(client, token, household):
    client.defaults["HTTP_AUTHORIZATION"] = f"Token {token.key}"
    client.defaults["HTTP_X_HOUSEHOLD"] = str(household.id)
    return client


@pytest.mark.django_db
def test_save_normalises_upc_a_to_ean13(cellar):
    user, household, _, _ = cellar
    wine = Wine.objects.create(
        user=user, household=household, name="UPC bottle",
        wine_type="RE", country="ES", barcode=UPC_A,
    )
    wine.refresh_from_db()
    assert wine.barcode == EAN13


@pytest.mark.django_db
def test_by_barcode_finds_upc_a_and_gtin14_of_stored_ean13(client, cellar):
    user, household, wine, token = cellar
    _auth(client, token, household)
    url = reverse("api-wine-by-barcode", kwargs={"ean": UPC_A})
    body = client.get(url).json()
    assert body["count"] == 1
    assert body["wines"][0]["id"] == wine.id

    url = reverse("api-wine-by-barcode", kwargs={"ean": GTIN14})
    body = client.get(url).json()
    assert body["count"] == 1
    assert body["wines"][0]["id"] == wine.id

    url = reverse("api-wine-by-barcode", kwargs={"ean": EAN13})
    body = client.get(url).json()
    assert body["count"] == 1
    assert body["wines"][0]["id"] == wine.id


@pytest.mark.django_db
def test_wine_scanned_finds_upc_a_of_stored_ean13(client, cellar):
    user, household, wine, token = cellar
    client.force_login(user)
    r = client.get(reverse("wine-scan", kwargs={"code": UPC_A}))
    assert r.status_code == 302
    assert r["Location"] == reverse("wine-detail", kwargs={"pk": wine.pk})
