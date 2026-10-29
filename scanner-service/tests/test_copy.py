"""Message catalog: English is the source of truth; every key needs Afrikaans."""

from integrations.copy import (
    MESSAGES,
    catalog_keys,
    locale_from_telegram,
    missing_af_keys,
    normalize_locale,
    t,
)


def test_every_english_key_has_afrikaans():
    assert missing_af_keys() == set()
    assert catalog_keys("en")
    assert catalog_keys("en") == catalog_keys("af")


def test_missing_locale_or_key_falls_back_to_english():
    assert t("xx", "invite.mint.fail") == t("en", "invite.mint.fail")
    assert t("en", "does.not.exist") == "does.not.exist"
    assert t("af", "invite.share.btn") == "Deel uitnodiging"


def test_locale_mapping():
    assert normalize_locale("en-gb") == "en"
    assert normalize_locale("en-US") == "en"
    assert normalize_locale("af") == "af"
    assert normalize_locale("af-ZA") == "af"
    assert normalize_locale("de-DE") == "en"
    assert normalize_locale(None) == "en"
    assert locale_from_telegram("af-ZA") == "af"
    assert locale_from_telegram(None) == "en"
    assert locale_from_telegram(object()) == "en"


def test_phase0_invite_keys_are_present():
    required = {
        "invite.join.share",
        "invite.share.btn",
        "invite.expiry.fallback",
        "invite.mint.fail",
        "invite.mint.bad_hh",
        "invite.fresh",
        "invite.unreachable",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert "30 days" in MESSAGES["en"]["invite.expiry.fallback"]
    assert "30 dae" in MESSAGES["af"]["invite.expiry.fallback"]


def test_phase1_list_keys_are_present():
    required = {
        "list.btn.add",
        "list.note.added",
        "list.note.already",
        "list.empty",
        "list.moved",
        "list.removed",
        "list.off",
        "list.title",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert t("en", "list.note.added") == "On your To-drink list."
    assert t("af", "list.note.added") == "Op jou To-drink-lys."


def test_phase3_social_keys_are_present():
    required = {
        "club.comment.btn",
        "club.comment.prompt",
        "club.comment.saved",
        "club.comment.too_long",
        "club.comment.again",
        "club.comment.more",
        "club.td.btn",
        "club.td.added",
        "club.to.title",
        "club.to.score",
        "club.to.closed",
        "club.to.need_two",
        "club.to.open",
        "club.to.close",
        "club.to.running",
        "club.to.summary_only",
        "club.to.already",
        "club.list.title",
        "club.list.empty",
        "club.td.off",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert t("en", "club.comment.btn") == "Say something"
    assert t("af", "club.comment.btn") == "Sê iets"
    assert t("en", "club.td.off") == "To-drink lists aren't on yet."
    assert t("af", "club.td.off") == "To-drink-lyste is nog nie aan nie."


def test_phase2_club_keys_are_present():
    required = {
        "club.off",
        "club.pitch",
        "club.hh.notice",
        "club.policy.full",
        "club.end.confirm",
        "club.widen.stay",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert "{name}" not in MESSAGES["en"]["club.hh.notice"]
    assert t("en", "club.off") == "Wine clubs aren't on yet."
    assert t("af", "club.off") == "Wynklubs is nog nie aan nie."


def test_phase4_global_keys_are_present():
    required = {
        "club.discover.title",
        "club.discover.btn",
        "club.global.consent",
        "club.global.private_cellar",
        "club.global.join",
        "club.global.not_now",
        "club.global.frozen",
        "club.global.kicked",
    }
    assert required <= catalog_keys("en")
    assert required <= catalog_keys("af")
    assert "Your cellar stays private" in t("en", "club.global.consent", name="X")
    assert "Jou kelder bly privaat" in t("af", "club.global.consent", name="X")
    assert t("en", "club.discover.btn") == "Discover"
    assert t("af", "club.discover.btn") == "Ontdek"
