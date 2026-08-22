"""
test_ssrf_guard.py — regression tests for the SSRF hardening of the URL fetcher.
===============================================================================

PRISM fetches URLs supplied by the user, which makes the fetcher an SSRF
primitive unless it is guarded: the attacker cannot reach our internal network,
but they can point our server at it, which is very nearly as useful. The classic
targets are the cloud metadata service (169.254.169.254), unauthenticated
services on the loopback interface, and `file://` for local disk.

Every test here is zero-network by construction:

* Scheme, credential and IP-literal rejections need no lookup at all — they are
  settled by parsing.
* The one case that needs DNS (a normal public hostname) injects a stub
  resolver, so the suite never depends on the machine having working DNS, on a
  third party being up, or on a particular IP staying assigned.
* Redirect and response-size behaviour is exercised against a fake HTTP client
  that records what was requested, so the real redirect loop in
  `ingestion._fetch_validated` is under test without a socket.

Run:
    pytest -q test_ssrf_guard.py

There is no pytest-only API in this file (no fixtures, no `pytest.raises`), so
it also runs standalone:
    python test_ssrf_guard.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import sourcing, urlguard

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_HOST_URL = "https://www.example-manufacturer.com/products/sensor-123"


def public_resolver(host):
    """Stub resolver: every name answers with one public address."""
    return [PUBLIC_IPV4]


def resolver_returning(*addresses):
    """Stub resolver with a fixed answer, for testing DNS-based rejection."""
    def _resolve(host):
        return list(addresses)
    return _resolve


def assert_rejected(url, expected_type=urlguard.UnsafeURLError, resolver=public_resolver):
    """Assert `url` is refused, and hand back the error for message checks.

    The resolver defaults to one that answers *publicly*, so a rejection can
    only come from the check under test — never from a lookup failure. That
    matters: a test that passes because DNS was broken is not testing anything.
    """
    try:
        urlguard.validate_url(url, resolver=resolver)
    except expected_type as exc:
        return exc
    except Exception as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"{url!r} was rejected, but with {type(exc).__name__} rather than "
            f"{expected_type.__name__}: {exc}"
        )
    raise AssertionError(f"{url!r} was ACCEPTED but should have been rejected")


def assert_fetch_refused(url, expected_status=400):
    """The app-level contract: a refusal is a normal result, not an exception.

    `fetch_and_clean_url` must hand back the same dict shape the success path
    returns, so `main.py` can render the reason instead of surfacing a 500 and a
    traceback. Imported lazily because this pulls in httpx, while the rest of
    the suite is stdlib-only.
    """
    from app.ingestion import fetch_and_clean_url

    result = fetch_and_clean_url(url)
    assert result["success"] is False, f"{url!r} was fetched: {result}"
    assert result["http_status"] == expected_status, \
        f"{url!r} gave HTTP {result['http_status']}, expected {expected_status}"
    assert result["error"], f"{url!r} was refused with no explanation"
    assert result["content_length"] == 0
    # Keys main.py reads unconditionally — a refusal must not KeyError.
    for key in ("text", "title", "source_type", "source_origin", "http_status"):
        assert key in result, f"refusal payload is missing {key!r}"
    return result


# ==========================================================================
# A. Loopback IPv4
# ==========================================================================
def test_a_rejects_loopback_ipv4():
    exc = assert_rejected("http://127.0.0.1", urlguard.BlockedAddressError)
    assert "loopback" in str(exc).lower()


def test_a_rejects_loopback_ipv4_with_port_and_path():
    # The realistic payload is a service on the loopback interface, not bare /.
    assert_rejected("http://127.0.0.1:6379/", urlguard.UnsafeURLError)
    assert_rejected("http://127.0.0.1:8000/admin", urlguard.BlockedAddressError)


def test_a_rejects_loopback_alternate_spellings():
    # Why a hostname/string blacklist cannot be the control: all of these are
    # 127.0.0.1, and none of them contain the text "127.0.0.1".
    for url in ("http://127.1", "http://0x7f.0.0.1", "http://2130706433"):
        try:
            urlguard.validate_url(url, resolver=public_resolver)
        except urlguard.UnsafeURLError:
            continue
        raise AssertionError(f"{url!r} (an alternate spelling of 127.0.0.1) was accepted")


def test_a_fetch_refuses_loopback_safely():
    result = assert_fetch_refused("http://127.0.0.1")
    assert "security policy" in result["error"].lower()


# ==========================================================================
# B. localhost
# ==========================================================================
def test_b_rejects_localhost():
    assert_rejected("http://localhost", urlguard.BlockedAddressError)


def test_b_rejects_localhost_with_port():
    assert_rejected("http://localhost:8080/status", urlguard.BlockedAddressError)


def test_b_rejects_localhost_even_if_dns_lies():
    # The name check is convenience; resolution is the control. Prove the
    # rejection does not depend on the resolver by giving it a public answer.
    assert_rejected("http://localhost", urlguard.BlockedAddressError,
                    resolver=resolver_returning(PUBLIC_IPV4))


def test_b_fetch_refuses_localhost_safely():
    assert_fetch_refused("http://localhost")


# ==========================================================================
# C. Private 10.0.0.0/8
# ==========================================================================
def test_c_rejects_private_10_range():
    exc = assert_rejected("http://10.0.0.1", urlguard.BlockedAddressError)
    assert "private" in str(exc).lower()


def test_c_rejects_private_10_range_boundaries():
    for url in ("http://10.0.0.0", "http://10.255.255.255", "http://10.4.5.6:8080/x"):
        assert_rejected(url, urlguard.BlockedAddressError)


def test_c_fetch_refuses_private_10_safely():
    assert_fetch_refused("http://10.0.0.1")


# ==========================================================================
# D. Private 192.168.0.0/16
# ==========================================================================
def test_d_rejects_private_192_168():
    exc = assert_rejected("http://192.168.1.1", urlguard.BlockedAddressError)
    assert "private" in str(exc).lower()


def test_d_rejects_other_private_ranges():
    # 172.16/12 is the range most often missed, because 172.x looks public.
    assert_rejected("http://172.16.0.1", urlguard.BlockedAddressError)
    assert_rejected("http://172.31.255.254", urlguard.BlockedAddressError)
    # ...and 172.32 really is public, so the mask must not be over-broad.
    assert urlguard.is_public_ip("172.32.0.1"), \
        "172.32.0.1 is public; a /12 mask applied as /8 would break real fetches"
    assert urlguard.is_public_ip("172.15.0.1")


def test_d_fetch_refuses_private_192_168_safely():
    assert_fetch_refused("http://192.168.1.1")


# ==========================================================================
# E. Cloud metadata endpoint
# ==========================================================================
def test_e_rejects_cloud_metadata():
    exc = assert_rejected("http://169.254.169.254", urlguard.BlockedAddressError)
    assert "metadata" in str(exc).lower() or "link-local" in str(exc).lower()


def test_e_rejects_cloud_metadata_credential_path():
    # The actual exploit URL.
    assert_rejected(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        urlguard.BlockedAddressError,
    )


def test_e_rejects_whole_link_local_range_not_just_the_one_address():
    assert_rejected("http://169.254.0.1", urlguard.BlockedAddressError)
    assert_rejected("http://169.254.169.253", urlguard.BlockedAddressError)


def test_e_rejects_other_providers_metadata_addresses():
    for address in ("100.100.100.200", "192.0.0.192"):
        assert not urlguard.is_public_ip(address), \
            f"{address} is a metadata endpoint and must not be considered public"


def test_e_fetch_refuses_metadata_safely():
    assert_fetch_refused("http://169.254.169.254/latest/meta-data/")


# ==========================================================================
# F. IPv6 loopback
# ==========================================================================
def test_f_rejects_ipv6_loopback():
    exc = assert_rejected("http://[::1]", urlguard.BlockedAddressError)
    assert "loopback" in str(exc).lower()


def test_f_rejects_ipv6_loopback_with_port():
    assert_rejected("http://[::1]:8080/admin", urlguard.BlockedAddressError)


def test_f_rejects_ipv6_private_and_link_local():
    assert_rejected("http://[fc00::1]", urlguard.BlockedAddressError)   # unique local
    assert_rejected("http://[fd00::1]", urlguard.BlockedAddressError)   # unique local
    assert_rejected("http://[fe80::1]", urlguard.BlockedAddressError)   # link-local
    assert_rejected("http://[::]", urlguard.BlockedAddressError)        # unspecified


def test_f_rejects_ipv4_mapped_ipv6_loopback():
    # ::ffff:127.0.0.1 is loopback in disguise: as an IPv6Address its
    # .is_loopback is False, so the address has to be unwrapped before checking.
    assert_rejected("http://[::ffff:127.0.0.1]", urlguard.BlockedAddressError)
    assert_rejected("http://[::ffff:10.0.0.1]", urlguard.BlockedAddressError)
    assert not urlguard.is_public_ip("::ffff:169.254.169.254")


def test_f_fetch_refuses_ipv6_loopback_safely():
    assert_fetch_refused("http://[::1]")


# ==========================================================================
# G. file://
# ==========================================================================
def test_g_rejects_file_scheme():
    exc = assert_rejected("file:///etc/passwd", urlguard.BlockedSchemeError)
    assert "scheme" in str(exc).lower()


def test_g_rejects_file_scheme_windows_path():
    assert_rejected("file://C:/Windows/win.ini", urlguard.BlockedSchemeError)


def test_g_file_url_is_not_rewritten_into_something_fetchable():
    # Regression guard for the scheme-defaulting convenience: the app prepends
    # https:// to a bare host, and must not do so to an opaque scheme.
    exc = assert_rejected("file:///etc/passwd", urlguard.BlockedSchemeError)
    assert "file" in str(exc).lower()


def test_g_fetch_refuses_file_scheme_safely():
    assert_fetch_refused("file:///etc/passwd")


# ==========================================================================
# H. ftp:// and other non-HTTP schemes
# ==========================================================================
def test_h_rejects_ftp_scheme():
    assert_rejected("ftp://example.com", urlguard.BlockedSchemeError)


def test_h_rejects_ftp_with_path_and_port():
    assert_rejected("ftp://example.com:21/pub/file.txt", urlguard.BlockedSchemeError)


def test_h_rejects_other_non_http_schemes():
    for url in (
        "gopher://example.com/1",
        "data://text/html,<h1>x</h1>",
        "data:text/html;base64,PHNjcmlwdD4=",
        "javascript://example.com/%0aalert(1)",
        "javascript:alert(1)",
        "dict://127.0.0.1:11211/stat",
        "ldap://example.com/",
        "sftp://example.com/etc",
        "vbscript:msgbox(1)",
        "telnet://example.com:23",
    ):
        try:
            urlguard.validate_url(url, resolver=public_resolver)
        except urlguard.UnsafeURLError:
            continue
        raise AssertionError(f"{url!r} uses a non-HTTP scheme but was accepted")


def test_h_allows_only_http_and_https():
    assert urlguard.ALLOWED_SCHEMES == frozenset({"http", "https"})


def test_h_fetch_refuses_ftp_safely():
    assert_fetch_refused("ftp://example.com")


# ==========================================================================
# I. Embedded credentials
# ==========================================================================
def test_i_rejects_embedded_credentials():
    exc = assert_rejected("https://user:password@example.com",
                          urlguard.CredentialsInURLError)
    assert "credential" in str(exc).lower()


def test_i_rejects_username_only():
    assert_rejected("https://user@example.com/path", urlguard.CredentialsInURLError)


def test_i_rejects_credentials_disguising_the_real_host():
    # The reason this matters: it reads like a request to the manufacturer, but
    # the host is 169.254.169.254.
    assert_rejected("https://www.moen.com@169.254.169.254/latest/meta-data/",
                    urlguard.CredentialsInURLError)


def test_i_at_sign_in_path_is_still_fine():
    # Must reject credentials without rejecting a legitimate '@' in the path.
    validated = urlguard.validate_url("https://example.com/team/@handle",
                                      resolver=public_resolver)
    assert validated.host == "example.com"


def test_i_fetch_refuses_credentials_safely():
    assert_fetch_refused("https://user:password@example.com")


# ==========================================================================
# J. A normal public HTTPS URL still works
# ==========================================================================
def test_j_accepts_normal_public_https_url():
    # Stub resolver: the point is that validation passes, not that a third party
    # is reachable from CI.
    validated = urlguard.validate_url(PUBLIC_HOST_URL, resolver=public_resolver)
    assert validated.scheme == "https"
    assert validated.host == "www.example-manufacturer.com"
    assert validated.addresses == [PUBLIC_IPV4]
    assert validated.is_ip_literal is False


def test_j_accepts_public_http_url():
    assert urlguard.is_safe_url("http://www.example-manufacturer.com/p/1",
                                resolver=public_resolver)


def test_j_accepts_real_manufacturer_urls_from_the_demo_set():
    # The URLs the app ships as sample chips must still pass validation.
    for url in (
        "https://www.phoenixcontact.com/en-us/products/sensor-actuator-cable-1533592",
        "https://www.te.com/usa-en/product-1-1987004-1.html",
        "https://www.moen.com/products/7594esrs",
    ):
        assert urlguard.is_safe_url(url, resolver=public_resolver), \
            f"{url} is a legitimate manufacturer URL and must pass validation"


def test_j_accepts_public_ip_literal():
    validated = urlguard.validate_url(f"http://{PUBLIC_IPV4}/x")
    assert validated.is_ip_literal is True


def test_j_bare_host_still_defaults_to_https():
    # Pre-existing convenience behaviour, preserved.
    validated = urlguard.validate_url("www.example-manufacturer.com/p/1",
                                      resolver=public_resolver)
    assert validated.scheme == "https"


def test_j_nonstandard_but_plausible_web_port_is_allowed():
    assert urlguard.is_safe_url("https://www.example-manufacturer.com:8443/p",
                                resolver=public_resolver)


# ==========================================================================
# K. Redirect to a private/unsafe destination
# ==========================================================================
class _FakeResponse:
    """Minimal stand-in for httpx.Response, streaming interface only."""

    def __init__(self, status_code, headers=None, chunks=(), encoding="utf-8"):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.encoding = encoding
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def iter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


class _FakeClient:
    """Records every URL requested, so 'never requested' is a real assertion."""

    def __init__(self, responses):
        self._responses = dict(responses)
        self.requested = []

    def stream(self, method, url, **kwargs):
        self.requested.append(url)
        try:
            return self._responses[url]
        except KeyError:  # pragma: no cover - diagnostic path
            raise AssertionError(
                f"fetcher requested an unexpected URL: {url!r} "
                f"(known: {sorted(self._responses)})"
            )


PUBLIC_START = "https://www.example-manufacturer.com/product/1"


def _fetch(client, start=PUBLIC_START):
    from app.ingestion import _fetch_validated
    # Stub resolver so redirect validation is tested without DNS, exactly as in
    # case J. The domains here are deliberately fake — a test must not depend on
    # a third party's DNS staying up.
    return _fetch_validated(client, start, "allow_all", resolver=public_resolver)


def test_k_redirect_to_loopback_is_rejected():
    unsafe = "http://127.0.0.1/admin"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(302, {"location": unsafe}),
        unsafe: _FakeResponse(200, chunks=[b"<html>should never be reached</html>"]),
    })
    try:
        _fetch(client)
    except urlguard.BlockedAddressError as exc:
        assert "loopback" in str(exc).lower()
    else:
        raise AssertionError("a redirect to 127.0.0.1 was followed")

    # The point of the manual redirect loop: the unsafe target is validated
    # *before* its request is built, so it is never requested at all.
    assert client.requested == [PUBLIC_START], \
        f"unsafe redirect target was requested: {client.requested}"


def test_k_redirect_to_cloud_metadata_is_rejected_before_request():
    unsafe = "http://169.254.169.254/latest/meta-data/"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(302, {"location": unsafe}),
        unsafe: _FakeResponse(200, chunks=[b"ASIA-SECRET-CREDENTIALS"]),
    })
    try:
        _fetch(client)
        raise AssertionError("a redirect to the metadata service was followed")
    except urlguard.BlockedAddressError:
        pass
    assert unsafe not in client.requested


def test_k_redirect_to_private_range_is_rejected():
    for unsafe in ("http://10.0.0.5/", "http://192.168.1.1/", "http://[::1]/"):
        client = _FakeClient({
            PUBLIC_START: _FakeResponse(302, {"location": unsafe}),
            unsafe: _FakeResponse(200, chunks=[b"internal"]),
        })
        try:
            _fetch(client)
            raise AssertionError(f"a redirect to {unsafe} was followed")
        except urlguard.BlockedAddressError:
            pass
        assert client.requested == [PUBLIC_START]


def test_k_redirect_to_non_http_scheme_is_rejected():
    unsafe = "file:///etc/passwd"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(302, {"location": unsafe}),
    })
    try:
        _fetch(client)
        raise AssertionError("a redirect to file:// was followed")
    except urlguard.BlockedSchemeError:
        pass


def test_k_relative_redirect_is_resolved_then_validated():
    # Location headers are routinely relative and must be joined before checking.
    target = "https://www.example-manufacturer.com/product/1/specs"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(302, {"location": "/product/1/specs"}),
        target: _FakeResponse(200, chunks=[b"<html><body>specs</body></html>"]),
    })
    outcome = _fetch(client)
    assert "specs" in outcome["html"]
    assert client.requested == [PUBLIC_START, target]


def test_k_safe_redirect_chain_is_still_followed():
    # Hardening must not break the ordinary http->https / bare->www redirect.
    hop = "https://www.example-manufacturer.com/en/product/1"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(301, {"location": hop}),
        hop: _FakeResponse(200, chunks=[b"<html><title>Sensor</title></html>"]),
    })
    outcome = _fetch(client)
    assert "Sensor" in outcome["html"]
    assert outcome["chain"] == [hop]


def test_k_redirect_loop_is_capped():
    # A server that redirects forever must not hang the worker.
    responses = {}
    for i in range(urlguard.MAX_REDIRECTS + 3):
        here = f"https://www.example-manufacturer.com/hop/{i}"
        responses[here] = _FakeResponse(
            302, {"location": f"https://www.example-manufacturer.com/hop/{i + 1}"})
    client = _FakeClient(responses)
    try:
        _fetch(client, "https://www.example-manufacturer.com/hop/0")
        raise AssertionError("an unbounded redirect loop was followed")
    except urlguard.TooManyRedirectsError:
        pass
    assert len(client.requested) <= urlguard.MAX_REDIRECTS + 1


def test_k_redirect_that_leaves_the_sourcing_policy_is_rejected():
    # Preserves the manufacturer/source rule across hops: a permitted source
    # must not be able to hand us off to a blocked one.
    from app.ingestion import _fetch_validated

    unsafe = "https://www.amazon.com/dp/B0001"
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(302, {"location": unsafe}),
        unsafe: _FakeResponse(200, chunks=[b"marketplace listing"]),
    })
    outcome = _fetch_validated(client, PUBLIC_START, "manufacturer_only",
                               resolver=public_resolver)
    assert "failure" in outcome, "a redirect into a marketplace was followed"
    assert outcome["failure"]["http_status"] == 403
    assert unsafe not in client.requested


# ==========================================================================
# L. Response larger than the configured maximum
# ==========================================================================
def test_l_read_capped_rejects_oversized_body():
    chunks = [b"x" * 1024] * 20          # 20 KiB
    try:
        urlguard.read_capped(chunks, max_bytes=4096)
        raise AssertionError("an oversized body was accepted")
    except urlguard.ResponseTooLargeError as exc:
        assert "limit" in str(exc).lower()


def test_l_read_capped_allows_body_at_the_limit():
    assert urlguard.read_capped([b"x" * 100], max_bytes=100) == b"x" * 100


def test_l_read_capped_stops_early_and_does_not_buffer_the_rest():
    consumed = {"chunks": 0}

    def endless():
        while True:
            consumed["chunks"] += 1
            yield b"y" * 1024

    try:
        urlguard.read_capped(endless(), max_bytes=8192)
        raise AssertionError("an unbounded stream was read to completion")
    except urlguard.ResponseTooLargeError:
        pass
    # Aborted promptly rather than draining the generator.
    assert consumed["chunks"] <= 12, f"read {consumed['chunks']} chunks before stopping"


def test_l_declared_content_length_is_rejected_before_reading():
    try:
        urlguard.check_declared_length(str(urlguard.MAX_RESPONSE_BYTES + 1))
        raise AssertionError("an oversized Content-Length was accepted")
    except urlguard.ResponseTooLargeError:
        pass


def test_l_missing_or_bogus_content_length_is_tolerated():
    # Header is optional and can be garbage; the byte counter is the real limit.
    urlguard.check_declared_length(None)
    urlguard.check_declared_length("")
    urlguard.check_declared_length("not-a-number")
    urlguard.check_declared_length("1024")


def test_l_fetcher_aborts_an_oversized_response():
    oversized = b"z" * (urlguard.MAX_RESPONSE_BYTES + 1024)
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(200, chunks=[oversized]),
    })
    try:
        _fetch(client)
        raise AssertionError("the fetcher accepted an oversized response")
    except urlguard.ResponseTooLargeError:
        pass


def test_l_fetcher_aborts_a_lying_content_length():
    # Declares 1 KiB, sends far more. The counter must still stop it.
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(
            200,
            {"content-length": "1024"},
            chunks=[b"q" * 65536] * ((urlguard.MAX_RESPONSE_BYTES // 65536) + 2),
        ),
    })
    try:
        _fetch(client)
        raise AssertionError("a response that lied about its length was accepted")
    except urlguard.ResponseTooLargeError:
        pass


def test_l_normal_sized_page_is_unaffected():
    client = _FakeClient({
        PUBLIC_START: _FakeResponse(
            200, {"content-length": "48"},
            chunks=[b"<html><title>Sensor</title><body>24 VDC</body></html>"]),
    })
    outcome = _fetch(client)
    assert "24 VDC" in outcome["html"]


def test_l_size_cap_is_configured_sensibly():
    assert 0 < urlguard.MAX_RESPONSE_BYTES <= 64 * 1024 * 1024


# ==========================================================================
# Timeouts (requirement 9)
# ==========================================================================
def test_timeouts_are_configured_and_finite():
    for name in ("CONNECT_TIMEOUT", "READ_TIMEOUT", "WRITE_TIMEOUT", "POOL_TIMEOUT"):
        value = getattr(urlguard, name)
        assert isinstance(value, (int, float)) and 0 < value <= 60, \
            f"{name} is not a sane finite timeout: {value!r}"


def test_fetcher_uses_the_granular_timeouts():
    from app import ingestion
    assert ingestion.HTTP_TIMEOUT.connect == urlguard.CONNECT_TIMEOUT
    assert ingestion.HTTP_TIMEOUT.read == urlguard.READ_TIMEOUT


def test_unresolvable_host_fails_closed():
    def broken_resolver(host):
        raise urlguard.UnresolvableHostError(f"no answer for {host}")

    assert_rejected("https://this-name-does-not-resolve.invalid",
                    urlguard.UnresolvableHostError, resolver=broken_resolver)


# ==========================================================================
# Requirement 7: resolution is the control, not the hostname string
# ==========================================================================
def test_hostname_that_resolves_to_a_private_address_is_rejected():
    # The case a string blacklist cannot catch: an ordinary-looking public name
    # whose DNS answer points inside the network.
    exc = assert_rejected("https://intranet.example-manufacturer.com/admin",
                          urlguard.BlockedAddressError,
                          resolver=resolver_returning("10.1.2.3"))
    assert "10.1.2.3" in str(exc)


def test_hostname_that_resolves_to_metadata_is_rejected():
    assert_rejected("https://totally-normal-name.com/",
                    urlguard.BlockedAddressError,
                    resolver=resolver_returning("169.254.169.254"))


def test_every_resolved_address_is_checked_not_just_the_first():
    # A rebinding-style answer: one public address and one loopback. httpx may
    # pick either, so any unsafe address in the set is disqualifying.
    assert_rejected("https://mixed-answers.example.com/",
                    urlguard.BlockedAddressError,
                    resolver=resolver_returning(PUBLIC_IPV4, "127.0.0.1"))


def test_blocked_internal_service_ports():
    for url in ("http://www.example-manufacturer.com:22/",
                "http://www.example-manufacturer.com:6379/",
                "http://www.example-manufacturer.com:3306/"):
        assert_rejected(url, urlguard.BlockedPortError)


def test_malformed_urls_are_rejected_not_crashed():
    for url in ("", "   ", "http://", "https://", "http:///path", None):
        try:
            urlguard.validate_url(url, resolver=public_resolver)
        except urlguard.UnsafeURLError:
            continue
        except Exception as exc:  # pragma: no cover - diagnostic path
            raise AssertionError(f"{url!r} raised {type(exc).__name__} instead of "
                                 f"an application-level UnsafeURLError: {exc}")
        raise AssertionError(f"{url!r} was accepted as a valid URL")


# ==========================================================================
# Requirement 11: the existing manufacturer/source policy still works
# ==========================================================================
NO_LIST = set()


def _verdict(url, policy="manufacturer_only"):
    return sourcing.evaluate_url(url, policy=policy, approved_domains=NO_LIST)


def test_sourcing_policy_is_preserved_for_manufacturers():
    verdict = _verdict("https://www.moen.com/products/7594esrs")
    assert verdict.allowed is True
    assert verdict.category == "manufacturer"


def test_sourcing_policy_is_preserved_for_marketplaces():
    verdict = _verdict("https://www.amazon.com/dp/B0001")
    assert verdict.allowed is False
    assert verdict.category == "marketplace"


def test_sourcing_policy_is_preserved_for_distributors():
    assert _verdict("https://www.grainger.com/product/123").allowed is False
    assert _verdict("https://www.grainger.com/product/123",
                    "allow_distributors").allowed is True


def test_sourcing_still_permits_unknown_domains_with_review_flag():
    verdict = _verdict("https://some-unknown-supplier.example/product/1")
    assert verdict.allowed is True
    assert verdict.category == "unknown"
    assert verdict.needs_review is True


def test_sourcing_rejects_non_http_schemes_regardless_of_policy():
    # Defense in depth: no sourcing policy, not even allow_all, may permit a
    # scheme that is not fetchable or a target on the loopback interface.
    for policy in ("manufacturer_only", "allow_distributors", "warn_only", "allow_all"):
        assert _verdict("file:///etc/passwd", policy).allowed is False, \
            f"file:// was permitted under {policy}"
        assert _verdict("http://127.0.0.1/", policy).allowed is False, \
            f"loopback was permitted under {policy}"


def test_sourcing_scheme_check_beats_the_allow_list():
    verdict = sourcing.evaluate_url("file:///etc/passwd",
                                    policy="manufacturer_only",
                                    approved_domains={"example.com"})
    assert verdict.allowed is False


# --------------------------------------------------------------------------
# Standalone runner, so this file also works without pytest installed.
# --------------------------------------------------------------------------
def _main():
    tests = sorted(
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}"))

    for name, why in failed:
        print(f"FAIL  {name}\n      {why}")
    print(f"\n{passed} passed, {len(failed)} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
