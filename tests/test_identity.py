"""Identity resolution: normalisation, role accounts, and link proposals."""

from __future__ import annotations

import pytest

from backend.pipeline.identity import (
    IdentityKind,
    MergePolicy,
    IdentityRecord,
    MatchSignal,
    ResolutionBand,
    band_for,
    extract_handles_from_signature,
    is_bulk_sender,
    is_common_name,
    is_public_domain,
    is_role_account,
    merge_display_names,
    name_key,
    names_agree,
    normalize_email,
    normalize_handle,
    normalize_phone,
    propose_link,
    propose_links,
)


def ident(id_, value, *names, kind=IdentityKind.EMAIL, role=False) -> IdentityRecord:
    return IdentityRecord(
        id=id_,
        kind=kind,
        value=normalize_email(value) if kind is IdentityKind.EMAIL else value,
        display_names=list(names),
        is_role=role,
    )


# --------------------------------------------------------------- email norm

def test_case_and_whitespace_folded():
    assert normalize_email("  John.Smith@Example.COM ") == "john.smith@example.com"


def test_angle_brackets_stripped():
    assert normalize_email("<bob@example.com>") == "bob@example.com"


def test_gmail_dots_are_insignificant():
    assert normalize_email("j.o.h.n@gmail.com") == "john@gmail.com"


def test_dots_are_significant_everywhere_else():
    """Folding dots universally merges strangers on most mail servers."""
    assert normalize_email("j.o.h.n@example.com") == "j.o.h.n@example.com"


def test_plus_tags_stripped_for_providers_that_support_them():
    assert normalize_email("john+github@gmail.com") == "john@gmail.com"


def test_plus_kept_for_unknown_providers():
    assert normalize_email("john+tag@obscure-host.net") == "john+tag@obscure-host.net"


def test_domain_aliases_folded():
    assert normalize_email("a@googlemail.com") == "a@gmail.com"
    assert normalize_email("a@me.com") == "a@icloud.com"


# --------------------------------------------------------------- phone norm

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+1 (555) 010-2030", "+15550102030"),
        ("555-010-2030", "+15550102030"),
        ("15550102030", "+15550102030"),
        ("+44 20 7946 0958", "+442079460958"),
        ("00442079460958", "+442079460958"),
    ],
)
def test_phone_normalisation(raw, expected):
    assert normalize_phone(raw) == expected


def test_ambiguous_phone_is_refused_not_guessed():
    """Guessing a country code silently merges two different people."""
    assert normalize_phone("2079460958", default_region_code="44") is None


@pytest.mark.parametrize("raw", ["", "   ", "abc", "12"])
def test_unparseable_phone_returns_none(raw):
    assert normalize_phone(raw) is None


def test_imessage_handle_routes_by_shape():
    assert normalize_handle("Bob@Example.com") == "bob@example.com"
    assert normalize_handle("+1 555 010 2030") == "+15550102030"


# ------------------------------------------------------------- role accounts

@pytest.mark.parametrize(
    "email",
    [
        "noreply@github.com",
        "no-reply@stripe.com",
        "notifications@slack.com",
        "support@apple.com",
        "billing@aws.amazon.com",
        "mailer-daemon@google.com",
        "careers@company.com",
        "bounce+123@sendgrid.net",
    ],
)
def test_role_accounts_detected(email):
    assert is_role_account(email)


@pytest.mark.parametrize(
    "email",
    ["john.smith@university.edu", "sarah@gmail.com", "a.buendia@lab.org"],
)
def test_real_people_are_not_role_accounts(email):
    assert not is_role_account(email)


def test_role_accounts_never_link_to_anyone():
    """Otherwise the contact list fills with brands."""
    a = ident("i1", "noreply@github.com", "GitHub", role=True)
    b = ident("i2", "noreply@gitlab.com", "GitHub", role=True)
    assert propose_link(a, b) is None


def test_bulk_headers_identify_machine_mail():
    assert is_bulk_sender({"List-Unsubscribe": "<mailto:x@y.com>"})
    assert is_bulk_sender({"Precedence": "bulk"})
    assert is_bulk_sender({"Auto-Submitted": "auto-generated"})
    assert not is_bulk_sender({"Subject": "hello"})
    assert not is_bulk_sender(None)


# --------------------------------------------------------------------- names

def test_titles_stripped_from_name_key():
    assert name_key("Prof. John Smith") == "john smith"
    assert name_key("Dr Sarah Chen, PhD") == "sarah chen"


def test_names_agree_across_formats():
    assert names_agree("John Smith", "john smith")
    assert names_agree("Smith, John", "John Smith")
    assert names_agree("J. Smith", "John Smith")
    assert names_agree("Prof. John Smith", "John Smith")


def test_different_people_do_not_agree():
    assert not names_agree("John Smith", "Jane Smith")
    assert not names_agree("John Smith", "John Smyth")


def test_single_token_names_never_produce_a_link():
    """'Mom' on two handles is too weak to merge on.

    The signal is still generated (so a user could lower the threshold), but
    it scores as a common name and falls below the ignore floor.
    """
    a = ident("i1", "mom@gmail.com", "Mom")
    b = ident("i2", "+15550102030", "Mom", kind=IdentityKind.PHONE)
    proposal = propose_link(a, b)
    assert proposal.signal is MatchSignal.COMMON_NAME
    assert proposal.band is ResolutionBand.IGNORE
    assert propose_links([a, b]) == []


def test_common_surnames_are_weak_evidence():
    assert is_common_name("John Smith")
    assert is_common_name("Wei Chen")
    assert not is_common_name("Aurelio Buendia")


def test_single_names_treated_as_common():
    assert is_common_name("Alex")
    assert is_common_name("")


def test_public_domains_identified():
    assert is_public_domain("gmail.com")
    assert not is_public_domain("university.edu")


# ---------------------------------------------------------------- proposals

def test_identical_handles_score_highest_but_still_ask():
    a = ident("i1", "john@example.com", "John")
    b = ident("i2", "JOHN@example.com", "J. Smith")
    p = propose_link(a, b)
    assert p.signal is MatchSignal.SAME_IDENTITY
    assert p.confidence == 1.0
    assert p.band is ResolutionBand.SUGGEST


def test_same_name_at_shared_private_domain_auto_links_band_check():
    a = ident("i1", "j.smith@university.edu", "John Smith")
    b = ident("i2", "john.smith@university.edu", "John Smith")
    p = propose_link(a, b)
    assert p.signal is MatchSignal.NAME_AND_DOMAIN
    assert p.band is ResolutionBand.SUGGEST  # 0.80 — below auto-link


def test_shared_public_domain_is_not_evidence():
    """Two John Smiths on gmail.com are almost certainly not one person."""
    a = ident("i1", "jsmith1@gmail.com", "John Smith")
    b = ident("i2", "jsmith2@gmail.com", "John Smith")
    p = propose_link(a, b)
    assert p.signal is MatchSignal.COMMON_NAME
    assert p.band is ResolutionBand.IGNORE


def test_rare_name_across_providers_is_suggested_not_merged():
    a = ident("i1", "abuendia@lab.org", "Aurelio Buendia")
    b = ident("i2", "aurelio@gmail.com", "Aurelio Buendia")
    p = propose_link(a, b)
    assert p.signal is MatchSignal.RARE_NAME
    assert p.band is ResolutionBand.SUGGEST


def test_signature_handle_is_strong_evidence():
    a = ident("i1", "j.smith@university.edu", "John Smith")
    b = ident("i2", "jsmith@gmail.com", "John Smith")
    p = propose_link(a, b, signature_handles={"jsmith@gmail.com"})
    assert p.signal is MatchSignal.SIGNATURE_BLOCK
    assert p.confidence == 0.85


def test_unrelated_identities_produce_nothing():
    a = ident("i1", "john@example.com", "John Smith")
    b = ident("i2", "sarah@other.org", "Sarah Chen")
    assert propose_link(a, b) is None


def test_identity_never_links_to_itself():
    a = ident("i1", "john@example.com", "John")
    assert propose_link(a, a) is None


# ------------------------------------------------------------------- bands

@pytest.mark.parametrize("confidence", [1.00, 0.98, 0.90, 0.85, 0.70, 0.60])
def test_nothing_ever_auto_links_by_default(confidence):
    """Shipped policy: every merge is the user's call, however certain."""
    assert band_for(confidence) is ResolutionBand.SUGGEST


@pytest.mark.parametrize("confidence", [0.59, 0.30, 0.0])
def test_weak_evidence_is_still_dropped(confidence):
    """Always-ask must not mean burying the user in noise."""
    assert band_for(confidence) is ResolutionBand.IGNORE


@pytest.mark.parametrize(
    "policy,confidence,expected",
    [
        (MergePolicy.CONSERVATIVE, 1.00, ResolutionBand.AUTO_LINK),
        (MergePolicy.CONSERVATIVE, 0.90, ResolutionBand.AUTO_LINK),
        (MergePolicy.CONSERVATIVE, 0.85, ResolutionBand.SUGGEST),
        (MergePolicy.MODERATE, 0.85, ResolutionBand.AUTO_LINK),
        (MergePolicy.MODERATE, 0.80, ResolutionBand.AUTO_LINK),
        (MergePolicy.MODERATE, 0.70, ResolutionBand.SUGGEST),
        (MergePolicy.MODERATE, 0.59, ResolutionBand.IGNORE),
    ],
)
def test_opt_in_policies_relax_the_rule(policy, confidence, expected):
    assert band_for(confidence, policy) is expected


def test_proposals_carry_the_policy_into_their_band():
    a = ident("i1", "john@example.com", "John Smith")
    b = ident("i2", "JOHN@example.com", "J. Smith")
    assert propose_link(a, b).band is ResolutionBand.SUGGEST
    assert propose_link(a, b).needs_approval is True
    assert (
        propose_link(a, b, policy=MergePolicy.CONSERVATIVE).band
        is ResolutionBand.AUTO_LINK
    )


# ------------------------------------------------------------------ batching

def test_propose_links_blocks_and_ranks():
    identities = [
        ident("i1", "j.smith@university.edu", "John Smith"),
        ident("i2", "john.smith@university.edu", "John Smith"),
        ident("i3", "abuendia@lab.org", "Aurelio Buendia"),
        ident("i4", "aurelio@gmail.com", "Aurelio Buendia"),
        ident("i5", "noreply@github.com", "GitHub", role=True),
        ident("i6", "unrelated@nowhere.net", "Zed Xylophone"),
    ]
    proposals = propose_links(identities)

    pairs = {tuple(sorted((p.identity_id, p.other_identity_id))) for p in proposals}
    assert ("i1", "i2") in pairs
    assert ("i3", "i4") in pairs
    assert not any("i5" in p for p in pairs), "role accounts must be excluded"
    assert not any("i6" in p for p in pairs)

    confidences = [p.confidence for p in proposals]
    assert confidences == sorted(confidences, reverse=True)


def test_each_pair_proposed_once_despite_multiple_blocks():
    """Two identities sharing both a name token and a domain must not duplicate."""
    identities = [
        ident("i1", "j.smith@university.edu", "John Smith"),
        ident("i2", "john.smith@university.edu", "John Smith"),
    ]
    assert len(propose_links(identities)) == 1


# ---------------------------------------------------------------- signatures

def test_signature_handles_extracted():
    signature = """
    --
    John Smith | Associate Professor
    Department of Computer Science
    j.smith@university.edu | +1 (555) 010-2030
    """
    handles = extract_handles_from_signature(signature)
    assert "j.smith@university.edu" in handles
    assert "+15550102030" in handles


def test_empty_signature_is_safe():
    assert extract_handles_from_signature("") == set()


def test_display_names_accumulate_without_duplicates():
    names = merge_display_names(["John Smith", "J. Smith"], "Prof. Smith")
    assert names[0] == "Prof. Smith"
    assert len(names) == 3
    again = merge_display_names(names, "john smith")
    assert len(again) == 3, "case-insensitive dedupe"


def test_display_name_list_is_capped():
    many = [f"Name {i}" for i in range(50)]
    assert len(merge_display_names(many, "New")) == 10
