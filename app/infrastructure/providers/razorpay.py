import hashlib
import hmac
from typing import Any
import httpx

from app.core.config import settings

class ProviderConfigurationError(RuntimeError):
    pass

class RazorpayClient:
    def _check(self):
        if not settings.razorpay_key_id or not settings.razorpay_key_secret:
            raise ProviderConfigurationError("Razorpay is not configured")

    async def create_order(self, amount: int, currency: str, receipt: str | None = None, notes: dict | None = None) -> dict[str, Any]:
        self._check()
        payload = {"amount": amount, "currency": currency, "receipt": receipt}
        if notes: payload["notes"] = notes
        auth = (settings.razorpay_key_id, settings.razorpay_key_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{settings.razorpay_base_url}/orders", json=payload, auth=auth)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
        if not settings.razorpay_webhook_secret:
            raise ProviderConfigurationError("Razorpay webhook secret is not configured")
        expected = hmac.new(settings.razorpay_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature or "")
