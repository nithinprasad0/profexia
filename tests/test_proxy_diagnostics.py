"""Proxy diagnostic + client IP resolution. No external services needed.

All addresses are RFC 5737 documentation addresses; none belong to real users.
"""

import json
from types import SimpleNamespace

import pytest

from app.core import rate_limit
from app.core.config import settings

FORGED = "203.0.113.7"      # what an attacker puts in X-Forwarded-For
CLIENT = "198.51.100.20"    # the "real" client as seen by the edge
EDGE = "192.0.2.1"          # an edge proxy appending the connecting address
ALL_IPS = (FORGED, CLIENT, EDGE)

# Render behind Cloudflare, as reported: "<anything the client sent>, client, edge"
RENDER_LIKE = {"X-Forwarded-For": f"{FORGED}, {CLIENT}, {EDGE}", "CF-Connecting-IP": CLIENT,
               "True-Client-IP": CLIENT, "CF-Ray": "abc-SIN", "X-Forwarded-Proto": "https"}


@pytest.fixture
def diagnostics_on(monkeypatch):
    monkeypatch.setattr(settings, "proxy_diagnostics", True)


def test_diagnostic_disabled_by_default(client):
    assert settings.proxy_diagnostics is False
    assert client.get("/health/proxy").status_code == 404
    assert "/health/proxy" not in client.get("/openapi.json").text


def test_diagnostic_reports_structure_without_any_ip(client, diagnostics_on, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    r = client.get("/health/proxy", headers=RENDER_LIKE)
    assert r.status_code == 200
    body = r.json()
    text = json.dumps(body)
    for ip in ALL_IPS:
        assert ip not in text, "diagnostic must never echo IP addresses"
    assert body["x_forwarded_for"]["hops"] == 3
    assert body["ip_headers"]["cf-connecting-ip"]["position_in_xff_from_right"] == 2
    assert body["ip_headers"]["true-client-ip"]["equals_cf_connecting_ip"] is True
    assert body["trusted_proxy_count_candidates"]["1"]["equals_cf_connecting_ip"] is False
    assert body["trusted_proxy_count_candidates"]["2"]["equals_cf_connecting_ip"] is True
    # With TRUSTED_PROXY_COUNT=1 this Render-like chain resolves to the edge, not the client:
    assert body["current_config"]["resolved_equals_cf_connecting_ip"] is False
    assert body["marker_headers_present"]["cf-ray"] is True


def test_diagnostic_flags_forged_values_as_test_net(client, diagnostics_on):
    r = client.get("/health/proxy", headers={"X-Forwarded-For": FORGED, "CF-Connecting-IP": FORGED})
    body = r.json()
    assert body["ip_headers"]["cf-connecting-ip"]["kind"] == "test-net"
    assert body["x_forwarded_for"]["hop_kinds_left_to_right"] == ["test-net"]


def _req(headers: dict, peer: str = "10.0.0.5"):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={k.lower(): v for k, v in headers.items()})


def test_client_ip_header_takes_precedence(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", "cf-connecting-ip")
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    assert rate_limit.client_ip(_req(RENDER_LIKE)) == CLIENT


@pytest.mark.parametrize("value", [None, "", "not-an-ip", "1.2.3.4, 5.6.7.8", "<script>"])
def test_client_ip_header_invalid_falls_back(monkeypatch, value):
    monkeypatch.setattr(settings, "client_ip_header", "cf-connecting-ip")
    monkeypatch.setattr(settings, "trusted_proxy_count", 2)
    headers = {"X-Forwarded-For": f"{FORGED}, {CLIENT}, {EDGE}"}
    if value is not None:
        headers["CF-Connecting-IP"] = value
    assert rate_limit.client_ip(_req(headers)) == CLIENT  # TRUSTED_PROXY_COUNT=2 fallback


def test_client_ip_unchanged_without_header_setting(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", "")
    monkeypatch.setattr(settings, "trusted_proxy_count", 0)
    assert rate_limit.client_ip(_req(RENDER_LIKE, peer="10.9.8.7")) == "10.9.8.7"


def test_client_ip_header_normalises_ipv6(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", "cf-connecting-ip")
    assert rate_limit.client_ip(_req({"CF-Connecting-IP": "2001:DB8::0001"})) == "2001:db8::1"


# --- render.yaml must keep the configuration measured in production -----------------

def _render_env() -> dict[str, str]:
    """Minimal parser for `- key: X` / `value: Y` pairs (avoids a YAML dependency)."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "render.yaml").read_text(encoding="utf-8")
    pairs = re.findall(r"- key:\s*(\S+)\s*\n\s*value:\s*\"?([^\"\n#]+?)\"?\s*(?:#.*)?\n", text)
    return {k: v.strip() for k, v in pairs}


def test_render_yaml_matches_measured_production_config():
    env = _render_env()
    assert env["CLIENT_IP_HEADER"] == "cf-connecting-ip"
    assert env["TRUSTED_PROXY_COUNT"] == "3"
    assert env["PROXY_DIAGNOSTICS"] == "false"


@pytest.mark.parametrize("with_header", [True, False])
def test_render_config_resolves_measured_chain_to_client(monkeypatch, with_header):
    """Measured on Render: '<client-sent>, <client>, <cloudflare edge>, <render internal>'."""
    env = _render_env()
    monkeypatch.setattr(settings, "client_ip_header", env["CLIENT_IP_HEADER"])
    monkeypatch.setattr(settings, "trusted_proxy_count", int(env["TRUSTED_PROXY_COUNT"]))
    headers = {"X-Forwarded-For": f"{FORGED}, {CLIENT}, {EDGE}, 10.20.30.40"}
    if with_header:
        headers["CF-Connecting-IP"] = CLIENT
    assert rate_limit.client_ip(_req(headers)) == CLIENT
