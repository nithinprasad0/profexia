from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

class PaymentOrderCreate(BaseModel):
    amount: int = Field(gt=0, description="Amount in smallest currency unit, e.g. paise for INR")
    currency: str = Field(default="INR", min_length=3, max_length=10)
    receipt: str | None = Field(default=None, max_length=120)
    notes: dict[str, str] | None = None

    @field_validator("notes")
    @classmethod
    def _bounded_notes(cls, value: dict[str, str] | None):
        # Razorpay accepts at most 15 note keys of up to 256 characters each.
        if value is not None and (len(value) > 15 or any(len(k) > 256 or len(v) > 256 for k, v in value.items())):
            raise ValueError("notes may contain at most 15 entries of 256 characters")
        return value

class PaymentOrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    provider_order_id: str
    amount: int
    currency: str
    status: str
    receipt: str | None
    metadata: dict | None = Field(default=None, validation_alias="extra_metadata", serialization_alias="metadata")
    created_at: datetime

class CodeExecutionCreate(BaseModel):
    language_id: int = Field(gt=0)
    source_code: str = Field(min_length=1, max_length=100_000)
    stdin: str | None = Field(default=None, max_length=50_000)

class CodeExecutionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    provider: str
    provider_submission_id: str | None
    language_id: int
    status: str
    stdout: str | None
    stderr: str | None
    compile_output: str | None
    time: str | None
    memory: int | None
    created_at: datetime
    updated_at: datetime

class LiveKitTokenRequest(BaseModel):
    # Optional and informational only: the server derives the room from the
    # learning session (`profexia-session-<id>`). A different value is rejected.
    room_name: str | None = Field(default=None, min_length=1, max_length=120)
    ttl_minutes: int = Field(default=60, ge=5, le=240)

class LiveKitTokenResponse(BaseModel):
    livekit_url: str
    room_name: str
    identity: str
    token: str
    expires_in_minutes: int
