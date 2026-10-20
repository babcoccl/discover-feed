"""The outlet registry (config/outlets.yaml): loading, domain matching, unknown
domains (kind=unknown + country from the TLD) and user ``groups``."""

import pytest

from app.outlets import (
    KINDS,
    OutletRegistry,
    country_from_tld,
    outlets,
)

YAML = """
outlets:
- name: Wire One
  domains: [wire.example, news.wire.example]
  country: US
  kind: wire
  groups: [wire-svc, english]
- name: Local Two
  domains: [local.example]
  country: GB
  kind: local
"""


def registry(tmp_path):
    path = tmp_path / "outlets.yaml"
    path.write_text(YAML, encoding="utf-8")
    return OutletRegistry.load(path)


def test_load_and_lookup(tmp_path):
    reg = registry(tmp_path)
    o = reg.for_url("https://news.wire.example/story/1")
    assert o.name == "Wire One" and o.kind == "wire" and o.country == "US"
    assert o.groups == ["wire-svc", "english"]
    assert reg.for_url("https://local.example/x").kind == "local"
    # suffix match: an unlisted subdomain still finds its outlet
    assert reg.for_url("https://amp.local.example/x").name == "Local Two"
    # name_of is the unknown-host fallback (a readable domain, never the URL)
    assert reg.name_of("https://news.express.co.uk/x") == "express.co.uk"


def test_unknown_domains_get_kind_unknown_and_tld_country(tmp_path):
    reg = registry(tmp_path)
    o = reg.for_url("https://elmundo.es/x")
    assert o.kind == "unknown" and o.country == "ES"
    assert o.name == "elmundo.es"  # falls back to the registrable domain
    assert reg.for_url("https://madeup.example/x").country is None


def test_country_from_tld():
    assert country_from_tld("bbc.co.uk") == "GB"
    assert country_from_tld("spiegel.de") == "DE"
    assert country_from_tld("reuters.com") is None


def test_group_helpers(tmp_path):
    reg = registry(tmp_path)
    assert reg.group_domains(["wire-svc", "none"]) == {
        "wire-svc": ["wire.example", "news.wire.example"],
        "none": [],
    }
    labels = [reg.for_url("https://wire.example/x"), reg.for_url("https://nope.example/x")]
    assert reg.groups_covered(labels, ["wire-svc", "missing"]) == {"wire-svc"}


def test_duplicate_domain_is_rejected(tmp_path):
    path = tmp_path / "outlets.yaml"
    path.write_text(
        "outlets:\n- name: A\n  domains: [dup.example]\n- name: B\n  domains: [dup.example]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="dup\\.example"):
        OutletRegistry.load(path)


def test_bad_kind_is_rejected(tmp_path):
    path = tmp_path / "outlets.yaml"
    path.write_text(
        "outlets:\n- name: A\n  domains: [a.example]\n  kind: nonsense\n", encoding="utf-8"
    )
    with pytest.raises(ValueError):
        OutletRegistry.load(path)


def test_starter_list_loads():
    """The shipped config/outlets.yaml parses and covers the main kinds."""
    reg = outlets()
    assert reg is not None
    kinds = {o.kind for o in reg.outlets}
    assert {"wire", "national", "international", "local", "trade"} <= kinds
    assert all(k in KINDS for k in kinds)
    assert len(reg.outlets) >= 140  # the ~150-outlet starter list
