"""Which mail gets ingested."""

from __future__ import annotations

import pytest

from backend.pipeline.label_policy import (
    LabelMode,
    LabelPolicy,
    unknown_labels,
)


def default() -> LabelPolicy:
    return LabelPolicy.from_settings("default")


# ------------------------------------------------------------------ defaults

def test_inbox_mail_is_ingested():
    assert default().should_ingest(["INBOX", "UNREAD", "CATEGORY_PERSONAL"])


def test_sent_mail_is_ingested():
    """Load-bearing for 'who haven't I replied to' and for own promises."""
    assert default().should_ingest(["SENT"])


@pytest.mark.parametrize("label", ["SPAM", "TRASH", "DRAFT"])
def test_spam_trash_and_drafts_are_never_ingested(label):
    assert not default().should_ingest(["INBOX", label])


@pytest.mark.parametrize(
    "category", ["CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"]
)
def test_noise_tabs_are_excluded_by_default(category):
    assert not default().should_ingest(["INBOX", category])


def test_updates_category_is_kept():
    """Receipts, bills and shipping notices live here — real deadlines."""
    assert default().should_ingest(["INBOX", "CATEGORY_UPDATES"])


def test_archived_mail_is_not_ingested_by_default():
    """Auto-archived by a filter means no INBOX label."""
    assert not default().should_ingest(["CATEGORY_PERSONAL", "UNREAD"])


def test_labels_are_case_insensitive():
    assert default().should_ingest(["inbox"])
    assert not default().should_ingest(["inbox", "category_promotions"])


def test_empty_labels_are_not_ingested_by_default():
    assert not default().should_ingest([])


def test_blank_and_none_entries_are_ignored():
    assert default().should_ingest(["INBOX", "", "   "])


# ------------------------------------------------------------------ all mail

def test_all_mail_takes_everything_except_spam_and_trash():
    policy = LabelPolicy.from_settings("all_mail")
    assert policy.should_ingest(["CATEGORY_PROMOTIONS"])
    assert policy.should_ingest([])
    assert not policy.should_ingest(["SPAM"])
    assert not policy.should_ingest(["TRASH"])
    assert not policy.should_ingest(["DRAFT"])


# -------------------------------------------------------------------- custom

def test_custom_labels_are_honoured():
    policy = LabelPolicy.from_settings("custom", include_csv="INBOX,Work,Family")
    assert policy.should_ingest(["WORK"])
    assert policy.should_ingest(["family"])
    assert not policy.should_ingest(["CATEGORY_PERSONAL"])


def test_custom_mode_can_exclude_too():
    policy = LabelPolicy.from_settings(
        "custom", include_csv="INBOX", exclude_csv="Newsletters"
    )
    assert policy.should_ingest(["INBOX"])
    assert not policy.should_ingest(["INBOX", "NEWSLETTERS"])


def test_custom_mode_still_refuses_spam():
    policy = LabelPolicy.from_settings("custom", include_csv="SPAM,INBOX")
    assert not policy.should_ingest(["SPAM"])


def test_empty_custom_list_falls_back_to_defaults():
    """An empty picker must not silently ingest nothing."""
    policy = LabelPolicy.from_settings("custom", include_csv="")
    assert policy.should_ingest(["INBOX"])


def test_unrecognised_mode_falls_back_to_default():
    policy = LabelPolicy.from_settings("nonsense")
    assert policy.mode is LabelMode.DEFAULT
    assert policy.should_ingest(["INBOX"])


# ------------------------------------------------------------------ diagnostics

def test_reason_explains_inclusion_and_exclusion():
    policy = default()
    assert "INBOX" in policy.reason(["INBOX"])
    assert "CATEGORY_PROMOTIONS" in policy.reason(["INBOX", "CATEGORY_PROMOTIONS"])
    assert "SPAM" in policy.reason(["SPAM"])
    assert "no included label" in policy.reason(["CATEGORY_PERSONAL"])


def test_typo_in_a_custom_label_is_detectable():
    """A misspelled label matches nothing and silently loses mail."""
    missing = unknown_labels(["INBOX", "Wrok", "Family"], available=["Work", "Family"])
    assert missing == ["WROK"]


def test_system_labels_are_always_considered_known():
    assert unknown_labels(["INBOX", "SENT", "CATEGORY_UPDATES"], available=[]) == []


# ----------------------------------------------------------------- gmail query

def test_query_narrows_server_side_for_catch_up():
    query = default().gmail_query()
    assert "label:inbox" in query and "label:sent" in query
    assert "-label:spam" in query
    assert "-label:category_promotions" in query


def test_all_mail_query_only_excludes():
    query = LabelPolicy.from_settings("all_mail").gmail_query()
    assert "-label:spam" in query
    assert "label:inbox" not in query
