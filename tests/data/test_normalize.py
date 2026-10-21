from __future__ import annotations

import pytest

from pipelineos.data.normalize import (
    clean_company_name,
    company_key,
    company_name_key,
    email_key,
    is_freemail,
    name_key,
    normalize_email,
    normalize_name,
    normalize_phone,
    registrable_domain,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Ana@Acme.IO ", "ana@acme.io"),
        ("o'neil@acme.co.uk", "o'neil@acme.co.uk"),
        ("n/a", None),
        ("test@test", None),
        ("noreply@", None),
        ("", None),
        ("two@@acme.io", None),
    ],
)
def test_normalize_email(raw: str, expected: str | None) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize(
    ("email", "key"),
    [
        ("j.smith+ci@gmail.com", "jsmith@gmail.com"),
        ("jsmith@googlemail.com", "jsmith@gmail.com"),
        ("j.smith+quarry@acme.io", "j.smith@acme.io"),  # dots matter outside Gmail
        ("ana@acme.io", "ana@acme.io"),
    ],
)
def test_email_key(email: str, key: str) -> None:
    assert email_key(email) == key


@pytest.mark.parametrize(
    ("raw", "domain"),
    [
        ("https://www.acme.io/pricing", "acme.io"),
        ("http://acme.io", "acme.io"),
        ("www.acme.io", "acme.io"),
        ("ACME.IO", "acme.io"),
        ("shop.foo.co.uk", "foo.co.uk"),
        ("", None),
        ("   ", None),
        ("localhost", None),
        ("https://www.ledgerline.example/careers", "ledgerline.example"),
        ("example", None),
    ],
)
def test_registrable_domain(raw: str, domain: str | None) -> None:
    assert registrable_domain(raw) == domain


def test_company_key_prefers_website_then_work_email() -> None:
    assert company_key("https://acme.io", "ana@other.dev") == "acme.io"
    assert company_key("", "ana@eng.acme.io") == "acme.io"
    assert company_key("", "ana@gmail.com") is None
    assert company_key("", None) is None
    assert is_freemail("icloud.com") and not is_freemail("acme.io")


@pytest.mark.parametrize(
    ("raw", "e164"),
    [
        ("(415) 555-0134", "+14155550134"),
        ("415.555.0134", "+14155550134"),
        ("+1 415 555 0134", "+14155550134"),
        ("14155550134", "+14155550134"),
        ("+44 20 7946 0958", "+442079460958"),
        ("555-0134", None),
        ("", None),
    ],
)
def test_normalize_phone(raw: str, e164: str | None) -> None:
    assert normalize_phone(raw) == e164


def test_names() -> None:
    assert normalize_name("  JOHN   ") == "John"
    assert normalize_name("mary ann") == "Mary Ann"
    assert normalize_name("McKenzie") == "McKenzie"
    assert name_key("O'Neil-Smith") == "oneilsmith"


@pytest.mark.parametrize("raw", ["Acme Inc.", "ACME, Inc", "Acme LLC", "acme", "Acme Corp"])
def test_company_name_key(raw: str) -> None:
    assert company_name_key(raw) == "acme"


def test_clean_company_name() -> None:
    assert clean_company_name("ACME, Inc") == "Acme"
    assert clean_company_name("  DataDog   LLC ") == "DataDog"
