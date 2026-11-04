"""Privacy-safe description of the forwarding headers a request arrived with.

Used to determine TRUSTED_PROXY_COUNT / CLIENT_IP_HEADER from evidence on the
real hosting platform. The report contains *structure only*: header presence,
hop counts, the kind of each address (public/private/...), positions and
equality booleans. It never contains an IP address or any header value.

Spoofing check: send a request with forged headers containing addresses from
the documentation ranges (RFC 5737 TEST-NET, e.g. 203.0.113.7). If a header the
app would trust still contains a TEST-NET address, the client could forge it.

Enabled only with PROXY_DIAGNOSTICS=true. Remove/disable after use.
"""

import ipaddress

from fastapi import Request

from app.core.config import settings
from app.core.rate_limit import client_ip

TEST_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
IP_HEADERS = ("cf-connecting-ip", "true-client-ip", "x-real-ip", "x-client-ip", "fly-client-ip")
MARKER_HEADERS = ("cf-ray", "cf-ipcountry", "cdn-loop", "forwarded", "x-forwarded-proto", "x-forwarded-host",
                  "x-forwarded-port", "x-request-start", "rndr-id", "via")


def _parse(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def _kind(ip) -> str:
    if ip is None:
        return "invalid"
    if any(ip in net for net in TEST_NETS):
        return "test-net"
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_global:
        return "public"
    return "other"


def _position_from_right(parts: list, ip) -> int | None:
    if ip is None:
        return None
    for index, item in enumerate(reversed(parts), start=1):
        if item == ip:
            return index
    return None


def describe(request: Request) -> dict:
    raw_xff = request.headers.get("x-forwarded-for")
    xff = [_parse(p) for p in (raw_xff or "").split(",") if p.strip()]
    cf = _parse(request.headers.get("cf-connecting-ip"))

    ip_headers = {}
    for name in IP_HEADERS:
        value = request.headers.get(name)
        ip = _parse(value)
        ip_headers[name] = {
            "present": value is not None,
            "kind": _kind(ip) if value is not None else None,
            "position_in_xff_from_right": _position_from_right(xff, ip),
            "equals_cf_connecting_ip": (ip == cf) if (ip is not None and cf is not None) else None,
        }

    peer = _parse(request.client.host if request.client else None)
    resolved = _parse(client_ip(request))
    candidates = {}
    for n in range(1, 6):
        candidate = xff[-n] if len(xff) >= n else None
        candidates[str(n)] = {
            "exists": candidate is not None,
            "kind": _kind(candidate) if candidate is not None else None,
            "equals_cf_connecting_ip": (candidate == cf) if (candidate is not None and cf is not None) else None,
        }

    return {
        "note": "Structure only; no IP addresses or header values are included.",
        "x_forwarded_for": {
            "present": raw_xff is not None,
            "hops": len(xff),
            "hop_kinds_left_to_right": [_kind(ip) for ip in xff],
        },
        "ip_headers": ip_headers,
        "marker_headers_present": {name: request.headers.get(name) is not None for name in MARKER_HEADERS},
        "uvicorn_peer": {
            "kind": _kind(peer),
            "position_in_xff_from_right": _position_from_right(xff, peer),
            "equals_cf_connecting_ip": (peer == cf) if (peer is not None and cf is not None) else None,
        },
        "trusted_proxy_count_candidates": candidates,
        "current_config": {
            "trusted_proxy_count": settings.trusted_proxy_count,
            "client_ip_header": settings.client_ip_header or None,
            "resolved_client_kind": _kind(resolved),
            "resolved_equals_cf_connecting_ip": (resolved == cf) if (resolved is not None and cf is not None) else None,
            "resolved_position_in_xff_from_right": _position_from_right(xff, resolved),
        },
    }
