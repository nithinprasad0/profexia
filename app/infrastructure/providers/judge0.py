from typing import Any
import httpx
from app.core.config import settings

class Judge0Client:
    def _check(self):
        if not settings.judge0_base_url:
            raise RuntimeError("Judge0 is not configured")

    def _headers(self):
        return {settings.judge0_auth_header: settings.judge0_api_key} if settings.judge0_api_key else {}

    @staticmethod
    def _limits() -> dict[str, Any]:
        """Sandbox limits for each submission (omitted when set to 0 = server default)."""
        limits: dict[str, Any] = {}
        if settings.judge0_cpu_time_limit > 0:
            limits["cpu_time_limit"] = settings.judge0_cpu_time_limit
        if settings.judge0_wall_time_limit > 0:
            limits["wall_time_limit"] = settings.judge0_wall_time_limit
        if settings.judge0_memory_limit_kb > 0:
            limits["memory_limit"] = settings.judge0_memory_limit_kb
        return limits

    async def create_submission(self, language_id: int, source_code: str, stdin: str | None = None) -> dict[str, Any]:
        self._check()
        payload: dict[str, Any] = {"language_id": language_id, "source_code": source_code, **self._limits()}
        if stdin is not None: payload["stdin"] = stdin
        async with httpx.AsyncClient(timeout=settings.judge0_timeout_seconds) as client:
            response = await client.post(
                f"{settings.judge0_base_url.rstrip('/')}/submissions",
                json=payload, headers=self._headers(),
                params={"wait": "true" if settings.judge0_wait else "false"},
            )
            response.raise_for_status()
            return response.json()

    async def get_submission(self, token: str) -> dict[str, Any]:
        self._check()
        async with httpx.AsyncClient(timeout=settings.judge0_timeout_seconds) as client:
            response = await client.get(
                f"{settings.judge0_base_url.rstrip('/')}/submissions/{token}",
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()
