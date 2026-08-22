"""
urlguard.py — SSRF hardening for the server-side URL fetcher.
=============================================================

PRISM fetches URLs that a user supplies. Without a guard, that is a
Server-Side Request Forgery primitive: the attacker does not control our
network, but they control *where our server points*, which is very nearly as
good. The classic payloads are `http://169.254.169.254/latest/meta-data/`
(cloud credentials), `http://127.0.0.1:6379` (an unauthenticated Redis on the
loopback interface) and `file:///etc/passwd`.

Design notes
------------
* **Stdlib only.** `urllib.parse`, `ipaddress` and `socket`. No new dependency,
  and nothing here imports `httpx`, so the whole validation layer is importable
  and testable without a network stack.

* **Deny by IP, not by name.** A hostname blacklist is not a security control:
  `127.0.0.1` has effectively unlimited spellings (`0x7f.1`, `2130706433`,
  `127.1`, a DNS record that simply answers `127.0.0.1`). So the host is
  *resolved* and every address it resolves to is checked against the
  non-public ranges. The name-based checks that remain are convenience only.

* **Every redirect hop is a new decision.** A public URL that 302s to
  `http://169.254.169.254` is the standard bypass for a validate-once fetcher,
  so `follow_redirects` must stay off and each hop must be revalidated *before*
  its request is sent. See `ingestion.fetch_and_clean_url`.

* **Fail closed.** If a host cannot be resolved, or an address cannot be
  parsed, the URL is rejected rather than attempted.

Known residual risk: DNS rebinding. We validate the addresses a name resolves
to, then hand the *name* to httpx, which resolves it again — a hostile resolver
can answer differently the second time. Closing that fully means pinning the
validated IP into the connection and carrying the original Host header, which
requires a custom transport. It is deliberately out of scope here and recorded
rather than silently ignored.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Sequence

# --------------------------------------------------------------------------
# Policy constants. Single place to tune.
# --------------------------------------------------------------------------
ALLOWED_SCHEMES = frozenset({"http", "https"})

# Granular timeouts. A single total timeout is not enough: a hostile server can
# hold a socket open by trickling one byte at a time, which never trips a
# connect timeout.
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
WRITE_TIMEOUT = 5.0
POOL_TIMEOUT = 5.0

# Hard ceiling on the bytes we will pull from a remote server. A product page
# is tens of kilobytes; anything past a few megabytes is either a mistake or a
# decompression/bandwidth attack.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB

# Redirect chains are followed manually so each hop can be revalidated.
MAX_REDIRECTS = 5

# Ports that are never a product page but are common SSRF targets. This is a
# convenience filter layered on top of the IP checks, not a primary control.
BLOCKED_PORTS = frozenset({
    22, 23, 25, 110, 143, 445, 465, 587, 993, 995,   # admin / mail / smb
    1433, 1521, 3306, 5432, 6379, 9200, 11211, 27017,  # databases / caches
    2375, 2376, 10250,                                # docker / kubelet
})

# Cloud instance-metadata services. Requirement: 169.254.169.254 must be
# explicitly blocked. The IPv4 one is inside 169.254.0.0/16 and so is already
# caught as link-local, but naming it keeps the intent legible and covers the
# providers that use an address outside that range.
CLOUD_METADATA_ADDRESSES = frozenset({
    "169.254.169.254",   # AWS / GCP / Azure / DigitalOcean / OpenStack
    "100.100.100.200",   # Alibaba Cloud
    "192.0.0.192",       # Oracle Cloud
    "fd00:ec2::254",     # AWS IPv6
})

# Explicit network denylist. `ipaddress`'s own flags (is_private, is_loopback,
# is_link_local, is_multicast, is_reserved) already cover most of these; they
# are spelled out because the requirement names them, because it documents
# intent at review time, and because it does not depend on a stdlib flag
# keeping the same meaning across versions.
_BLOCKED_NETWORKS: Sequence[ipaddress._BaseNetwork] = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # ---- IPv4 ----
        "0.0.0.0/8",           # "this host on this network"
        "10.0.0.0/8",          # private
        "100.64.0.0/10",       # carrier-grade NAT
        "127.0.0.0/8",         # loopback
        "169.254.0.0/16",      # link-local, incl. cloud metadata
        "172.16.0.0/12",       # private
        "192.0.0.0/24",        # IETF protocol assignments
        "192.0.2.0/24",        # documentation
        "192.168.0.0/16",      # private
        "198.18.0.0/15",       # benchmarking
        "198.51.100.0/24",     # documentation
        "203.0.113.0/24",      # documentation
        "224.0.0.0/4",         # multicast
        "240.0.0.0/4",         # reserved
        "255.255.255.255/32",  # broadcast
        # ---- IPv6 ----
        "::/128",              # unspecified
        "::1/128",             # loopback
        "64:ff9b::/96",        # NAT64 — can embed a private IPv4
        "100::/64",            # discard-only
        "2001:db8::/32",       # documentation
        "fc00::/7",            # unique local (private)
        "fe80::/10",           # link-local
        "ff00::/8",            # multicast
    )
)

# Hostnames that are obviously local. Convenience only — the IP checks are the
# actual control, and these names are re-checked by resolution anyway.
_LOCAL_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
})

# Schemes that appear without a "//" authority. They matter because the app
# accepts bare hosts ("turck.com/x") and prepends https:// to them. Without
# this set, "javascript:alert(1)" would be rewritten to
# "https://javascript:alert(1)" and rejected for the wrong reason — and any
# such scheme added later would be silently rewritten instead of refused.
_OPAQUE_SCHEMES = frozenset({
    "javascript", "data", "vbscript", "file", "mailto", "tel", "sms",
    "about", "blob", "chrome", "view-source", "jar", "gopher", "ftp",
    "ldap", "dict", "sftp", "smb", "nfs", "telnet", "ws", "wss",
})


# --------------------------------------------------------------------------
# Errors. All application-level, so the caller can turn them into a useful
# message instead of a traceback.
# --------------------------------------------------------------------------
class SafeFetchError(Exception):
    """Base class for every refusal raised by this module."""


class UnsafeURLError(SafeFetchError):
    """The URL may not be fetched."""


class BlockedSchemeError(UnsafeURLError):
    """Scheme is not http/https."""


class CredentialsInURLError(UnsafeURLError):
    """URL carries an embedded username or password."""


class MalformedURLError(UnsafeURLError):
    """URL could not be parsed into something fetchable."""


class BlockedAddressError(UnsafeURLError):
    """Host is, or resolves to, a non-public address."""


class BlockedPortError(UnsafeURLError):
    """Port is not a plausible web port."""


class UnresolvableHostError(UnsafeURLError):
    """Host could not be resolved, so it cannot be validated."""


class TooManyRedirectsError(SafeFetchError):
    """Redirect chain exceeded MAX_REDIRECTS."""


class ResponseTooLargeError(SafeFetchError):
    """Remote server tried to send more than MAX_RESPONSE_BYTES."""


Resolver = Callable[[str], List[str]]


@dataclass
class ValidatedURL:
    """A URL that has passed every check, with what we learned along the way."""
    url: str
    scheme: str
    host: str
    port: Optional[int]
    addresses: List[str] = field(default_factory=list)
    is_ip_literal: bool = False

    @property
    def netloc(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


# --------------------------------------------------------------------------
# IP-level checks
# --------------------------------------------------------------------------
def _unwrap(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Unwrap IPv6 forms that tunnel an IPv4 address.

    `::ffff:127.0.0.1` is loopback wearing a hat: as an IPv6Address its
    `is_loopback` is False, so checking the flags without unwrapping first
    would wave it straight through.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
        teredo = ip.teredo
        if teredo is not None:
            # (server, client) — the client address is the interesting one.
            return teredo[1]
    return ip


def parse_ip_literal(host: str) -> Optional[ipaddress._BaseAddress]:
    """Parse `host` as an IP address, including the shorthand IPv4 forms.

    `ipaddress.ip_address` only accepts full dotted-quad, but the C resolver —
    and therefore every HTTP client — also accepts `127.1`, `0x7f.0.0.1` and
    `2130706433`. All three are 127.0.0.1. If we treated them as hostnames we
    would hand them to DNS, and a resolver that answers for them (or a client
    that short-circuits them) walks straight onto the loopback interface.
    `inet_aton` implements exactly the same shorthand rules the client uses.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # Only attempt shorthand when the string cannot be a real hostname: a
    # registrable name always contains a letter that is not a hex digit or 'x'.
    if not host or any(c not in "0123456789abcdefx." for c in host):
        return None
    try:
        packed = socket.inet_aton(host)
    except (OSError, UnicodeEncodeError):
        return None
    return ipaddress.IPv4Address(packed)


def describe_blocked_ip(raw: str) -> Optional[str]:
    """Return why `raw` is not publicly routable, or None if it is fine."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return f"'{raw}' is not a valid IP address"

    ip = _unwrap(ip)
    text = str(ip)

    if text in CLOUD_METADATA_ADDRESSES or raw in CLOUD_METADATA_ADDRESSES:
        return f"{text} is a cloud instance-metadata endpoint"

    for flag, label in (
        ("is_loopback", "a loopback address"),
        ("is_link_local", "a link-local address"),
        ("is_multicast", "a multicast address"),
        ("is_unspecified", "the unspecified address"),
        ("is_private", "a private address"),
        ("is_reserved", "a reserved address"),
    ):
        if getattr(ip, flag, False):
            return f"{text} is {label}"

    for net in _BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return f"{text} falls inside the blocked range {net}"

    # Belt and braces: anything the stdlib does not consider globally routable.
    if not getattr(ip, "is_global", True):
        return f"{text} is not a globally routable address"

    return None


def is_public_ip(raw: str) -> bool:
    """True when `raw` is a parseable, publicly routable address."""
    return describe_blocked_ip(raw) is None


def resolve_host(host: str) -> List[str]:
    """Resolve `host` to every address it answers with (A and AAAA)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise UnresolvableHostError(
            f"'{host}' could not be resolved ({exc.__class__.__name__}), so it "
            f"cannot be verified as a public host."
        ) from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise UnresolvableHostError(f"'{host}' resolved to no addresses.")
    return addresses


# --------------------------------------------------------------------------
# URL-level checks
# --------------------------------------------------------------------------
def validate_url(
    url: str,
    *,
    resolver: Optional[Resolver] = None,
    resolve: bool = True,
) -> ValidatedURL:
    """Validate `url` for server-side fetching, or raise `UnsafeURLError`.

    Order matters. Scheme and credentials are rejected before any name lookup,
    so a `file://` URL never triggers network activity of any kind.

    Set `resolve=False` to run the parse-only checks (scheme, credentials,
    port, IP literals) and skip DNS — used to gate a URL before the sourcing
    policy runs, so a source that policy will reject costs us zero lookups.
    """
    if not isinstance(url, str) or not url.strip():
        raise MalformedURLError("No URL was supplied.")

    raw = url.strip()

    # A bare "example.com/x" is a convenience the app already supported, so a
    # missing scheme is still defaulted to https. But only when the string does
    # not already carry a scheme — otherwise "file:///etc/passwd" would be
    # rewritten into something fetchable, and an opaque form such as
    # "javascript:alert(1)" would be rejected for the wrong reason.
    if "://" not in raw:
        prefix = raw.split(":", 1)[0].lower() if ":" in raw else ""
        if prefix not in _OPAQUE_SCHEMES:
            raw = "https://" + raw

    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError as exc:
        raise MalformedURLError(f"URL could not be parsed: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedSchemeError(
            f"Scheme '{scheme or '(none)'}' is not permitted. Only "
            f"{', '.join(sorted(ALLOWED_SCHEMES))} URLs may be fetched."
        )

    # Embedded credentials. Checked on the raw netloc too, so a malformed
    # authority that urlsplit does not split into username/password still trips.
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise CredentialsInURLError(
            "URL contains embedded credentials (user:password@host). Refusing "
            "to fetch: credentials in a URL are frequently used to disguise the "
            "real host, and would be forwarded to it."
        )

    try:
        host = parsed.hostname
    except ValueError as exc:
        raise MalformedURLError(f"URL has an invalid host: {exc}") from exc

    if not host:
        raise MalformedURLError(f"URL '{url}' has no host component.")

    host = host.strip().rstrip(".").lower()
    if not host:
        raise MalformedURLError(f"URL '{url}' has an empty host component.")

    try:
        port = parsed.port
    except ValueError as exc:
        raise MalformedURLError(f"URL has an invalid port: {exc}") from exc

    if port is not None and port in BLOCKED_PORTS:
        raise BlockedPortError(
            f"Port {port} is not a web port and is a common internal-service "
            f"target; refusing to fetch."
        )

    # Is the host already an IP address? Then there is nothing to resolve and we
    # can check it directly. `urlsplit` strips the brackets from IPv6, and
    # `parse_ip_literal` also catches the shorthand forms of 127.0.0.1.
    literal = parse_ip_literal(host)

    if literal is not None:
        reason = describe_blocked_ip(str(literal))
        if reason is not None:
            raise BlockedAddressError(
                f"Refusing to fetch '{url}': {reason}. Only publicly routable "
                f"addresses may be fetched."
            )
        return ValidatedURL(
            url=raw, scheme=scheme, host=host, port=port,
            addresses=[str(_unwrap(literal))], is_ip_literal=True,
        )

    # Name-based convenience check. Not the control — resolution below is.
    if host in _LOCAL_HOSTNAMES or host.endswith(".localhost"):
        raise BlockedAddressError(
            f"Refusing to fetch '{url}': '{host}' names the local machine."
        )

    if not resolve:
        return ValidatedURL(url=raw, scheme=scheme, host=host, port=port)

    lookup: Resolver = resolver or resolve_host
    addresses = lookup(host)

    # Every address, not just the first. A host that answers with one public
    # and one loopback address is a rebinding attempt, and httpx may pick
    # either one.
    for address in addresses:
        reason = describe_blocked_ip(address)
        if reason is not None:
            raise BlockedAddressError(
                f"Refusing to fetch '{url}': host '{host}' resolves to "
                f"{address}, and {reason}."
            )

    return ValidatedURL(
        url=raw, scheme=scheme, host=host, port=port, addresses=list(addresses),
    )


def is_safe_url(url: str, *, resolver: Optional[Resolver] = None) -> bool:
    """Boolean form of `validate_url`, for call sites that want no exception."""
    try:
        validate_url(url, resolver=resolver)
        return True
    except SafeFetchError:
        return False


def resolve_redirect_target(base_url: str, location: str) -> str:
    """Join a `Location` header against the URL it came from.

    Redirect targets are routinely relative (`/en/product/123`), so they must be
    joined before they can be validated.
    """
    if not location or not location.strip():
        raise MalformedURLError("Redirect response had an empty Location header.")
    return urllib.parse.urljoin(base_url, location.strip())


# --------------------------------------------------------------------------
# Response-size cap
# --------------------------------------------------------------------------
def check_declared_length(
    content_length: Optional[object],
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> None:
    """Reject on the `Content-Length` header before reading the body.

    An advisory check: the header is optional and can lie, which is why
    `read_capped` still counts the bytes actually received.
    """
    if content_length in (None, ""):
        return
    try:
        declared = int(content_length)
    except (TypeError, ValueError):
        return
    if declared > max_bytes:
        raise ResponseTooLargeError(
            f"Remote server declared {declared} bytes, over the "
            f"{max_bytes}-byte limit; refusing to download it."
        )


def read_capped(
    chunks: Iterable[bytes],
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> bytes:
    """Concatenate `chunks`, aborting as soon as `max_bytes` is exceeded.

    Deliberately takes any iterable of bytes rather than an httpx response, so
    the cap is unit-testable without a network stack and works with any client.
    Iteration stops at the limit — we never buffer the oversized remainder.
    """
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    buffer = bytearray()
    for chunk in chunks:
        if not chunk:
            continue
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ResponseTooLargeError(
                f"Response exceeded the {max_bytes}-byte limit "
                f"(received more than {len(buffer)} bytes); download aborted."
            )
    return bytes(buffer)
