from datetime import datetime, timedelta, timezone
import jwt
from app.core.config import settings

class LiveKitClient:
    def _check(self):
        if not settings.livekit_api_key or not settings.livekit_api_secret or not settings.livekit_url:
            raise RuntimeError("LiveKit is not configured")

    def create_access_token(self, identity: str, room_name: str, can_publish: bool, can_subscribe: bool, ttl_minutes: int = 60) -> str:
        self._check()
        now = datetime.now(timezone.utc)
        payload = {
            "iss": settings.livekit_api_key,
            "sub": identity,
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=ttl_minutes)).timestamp()),
            "video": {
                "room": room_name,
                "roomJoin": True,
                "canPublish": can_publish,
                "canPublishData": True,  # in-room data messages (all participants)
                "canSubscribe": can_subscribe,
                # Never grant room administration or room creation/listing to clients.
                "roomAdmin": False,
                "roomCreate": False,
                "roomList": False,
            },
        }
        return jwt.encode(payload, settings.livekit_api_secret, algorithm="HS256")
