"""Every fetch verifies against certifi's roots, not the interpreter's default.

THE INCIDENT. EY (successfactors, 150 open rows) failed in 10 of the last 14
full runs and HSBC (sitemap, 25 open rows, 25 of them campus) in 8, both with
the identical message:

    <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
     unable to get local issuer certificate>

That is not a statement about either site. Both certificates chain to
Sectigo's "Public Server Authentication Root R46", which is present in
certifi and absent from macOS's `/etc/ssl/cert.pem` — the bundle
`ssl.get_default_verify_paths()` resolves to on this host. Confirmed by
direct handshake against both hosts on 2026-09-01: system bundle fails,
certifi succeeds. 175 open rows had been frozen for two weeks behind a stale
trust store on our own machine.

`successfactors.py`'s docstring had already worked this out and prescribed
exporting `SSL_CERT_FILE` — which nobody does before a cron run. These tests
pin the fix as the default instead of as a thing to remember, and pin that it
is a CHANGE OF ROOTS, never a relaxation: verification and hostname checking
stay on.
"""

from __future__ import annotations

import ssl

import certifi
import pytest

from coverage_connectors import http


def test_the_shared_context_verifies_and_checks_hostnames():
    assert http.SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    assert http.SSL_CONTEXT.check_hostname is True


def test_the_shared_context_is_built_from_certifi():
    """Compare loaded roots rather than a file path: what matters is which
    certificates are trusted, and the context does not remember where they
    came from."""
    from_certifi = ssl.create_default_context(cafile=certifi.where())
    assert (sorted(c["serialNumber"] for c in http.SSL_CONTEXT.get_ca_certs())
            == sorted(c["serialNumber"] for c in from_certifi.get_ca_certs()))


def test_the_certifi_bundle_carries_the_root_the_system_store_lacks():
    """The specific root EY and HSBC chain to. If a future certifi drops it,
    this test says so before two more boards go dark for a fortnight."""
    subjects = {
        value
        for cert in http.SSL_CONTEXT.get_ca_certs()
        for rdn in cert.get("subject", ())
        for key, value in rdn
        if key == "commonName"
    }
    assert any("Sectigo Public Server Authentication Root" in s for s in subjects), (
        "certifi no longer carries the Sectigo Public Server Authentication "
        "root that careers.ey.com and apply.careers.hsbc.com chain to"
    )


def test_every_request_is_made_with_that_context(monkeypatch):
    """The context is useless if `_do_request` forgets to pass it. Pinned
    because the whole EY/HSBC failure was one missing argument."""
    seen = {}

    class _Resp:
        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(http.urllib.request, "urlopen", fake_urlopen)
    http.fetch_bytes("https://example.invalid/jobs")
    assert seen["context"] is http.SSL_CONTEXT


def test_a_stripped_install_degrades_to_verifying_not_to_nothing(monkeypatch):
    """certifi is a declared dependency, but if it ever goes missing the
    fallback must still be a VERIFYING context. There is no branch here that
    turns verification off, and a test says so out loud."""
    import builtins

    real_import = builtins.__import__

    def no_certifi(name, *args, **kwargs):
        if name == "certifi":
            raise ImportError("no certifi")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_certifi)
    ctx = http._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
