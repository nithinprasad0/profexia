import hashlib
import hmac
import jwt
from app.main import app
from tests.helpers import route_paths
from app.core.config import settings
from app.infrastructure.providers.razorpay import RazorpayClient
from app.infrastructure.providers.livekit import LiveKitClient

def test_external_provider_routes_exist():
    paths = route_paths(app)
    expected = {
        "/api/v1/payments/orders",
        "/api/v1/payments/webhooks/razorpay",
        "/api/v1/code-executions",
        "/api/v1/code-executions/{execution_id}",
        "/api/v1/learning-sessions/{session_id}/livekit-token",
    }
    assert expected.issubset(paths)

def test_external_provider_openapi():
    paths = app.openapi()["paths"]
    assert "/api/v1/payments/orders" in paths
    assert "/api/v1/payments/webhooks/razorpay" in paths
    assert "/api/v1/code-executions" in paths
    assert "/api/v1/learning-sessions/{session_id}/livekit-token" in paths

def test_razorpay_webhook_signature(monkeypatch):
    secret = "test-webhook-secret"
    monkeypatch.setattr(settings, "razorpay_webhook_secret", secret)
    body = b'{"event":"payment.captured"}'
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert RazorpayClient.verify_webhook_signature(body, signature)
    assert not RazorpayClient.verify_webhook_signature(body, "bad")

def test_livekit_token_shape(monkeypatch):
    monkeypatch.setattr(settings, "livekit_api_key", "test-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "test-secret")
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    token = LiveKitClient().create_access_token("42", "room-1", True, True, 60)
    payload = jwt.decode(token, "test-secret", algorithms=["HS256"], options={"verify_aud": False})
    assert payload["iss"] == "test-key"
    assert payload["sub"] == "42"
    assert payload["video"]["room"] == "room-1"
    assert payload["video"]["roomJoin"] is True
