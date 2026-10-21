"""Field normalizers: turn the many spellings of one value into a single comparable key.

Each function is total (never raises on bad input) and returns ``None`` when the value cannot be
salvaged, so the caller decides whether that rejects the row or just leaves a field blank.
Domain parsing uses tldextract's bundled public-suffix snapshot with no network fetch, so
``foo.co.uk`` resolves to ``foo.co.uk`` and ``https://www.acme.io/pricing`` to ``acme.io``.
"""

from __future__ import annotations

import re

import tldextract

FREEMAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "icloud.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
    }
)
_GMAIL = frozenset({"gmail.com", "googlemail.com"})
_EMAIL_RE = re.compile(r"^[a-z0-9._%+\-']+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}$")
_COMPANY_SUFFIX_RE = re.compile(
    r"[,\s]+(inc|inc\.|llc|l\.l\.c\.|ltd|ltd\.|corp|corp\.|co|co\.|gmbh|plc)\.?$", re.IGNORECASE
)
_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def normalize_email(raw: str) -> str | None:
    """Trimmed, lower-cased address, or ``None`` if it is not a plausible email."""
    email = raw.strip().lower()
    return email if _EMAIL_RE.match(email) else None


def email_key(email: str) -> str:
    """Identity key for an already-normalized address.

    Drops ``+tags`` everywhere and dots in Gmail local parts, where Google ignores them, so
    ``j.smith+ci@gmail.com`` and ``jsmith@gmail.com`` share a key.
    """
    local, _, domain = email.partition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


def email_domain(email: str) -> str:
    return email.rpartition("@")[2]


def is_freemail(domain: str) -> bool:
    return domain in FREEMAIL


def registrable_domain(raw: str) -> str | None:
    """``https://www.acme.io/pricing`` -> ``acme.io``; ``None`` for anything without a suffix.

    ``.example`` (reserved by RFC 2606, never a real business) counts as a suffix, so demo data
    and fixtures can use ``ledgerline.example`` instead of a domain someone may own.
    """
    value = raw.strip().lower()
    if not value:
        return None
    extracted = _EXTRACT(value)
    domain = extracted.top_domain_under_public_suffix
    if not domain and extracted.domain == "example" and extracted.subdomain:
        domain = f"{extracted.subdomain.rsplit('.', 1)[-1]}.example"
    return domain or None


def company_key(website: str, email: str | None) -> str | None:
    """The company's registrable domain: from the website, else from a work email."""
    key = registrable_domain(website)
    if key:
        return key
    if email:
        domain = email_domain(email)
        if not is_freemail(domain):
            return registrable_domain(domain)
    return None


def normalize_phone(raw: str) -> str | None:
    """North American numbers to E.164 (``+14155550134``); other ``+`` numbers kept as digits."""
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and not digits.startswith("1") and len(digits) >= 8:
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def normalize_name(raw: str) -> str:
    """Collapse whitespace; fix ALL CAPS / all lower, but keep deliberate mixed case (McKenzie)."""
    name = " ".join(raw.split())
    if name.isupper() or name.islower():
        return name.title()
    return name


def name_key(raw: str) -> str:
    return re.sub(r"[^a-z]", "", raw.lower())


def company_name_key(raw: str) -> str:
    """``ACME, Inc`` / ``Acme LLC`` / ``acme`` -> ``acme``."""
    name = _COMPANY_SUFFIX_RE.sub("", raw.strip())
    return re.sub(r"[^a-z0-9]", "", name.lower())


def clean_company_name(raw: str) -> str:
    name = _COMPANY_SUFFIX_RE.sub("", " ".join(raw.split()))
    return normalize_name(name)
