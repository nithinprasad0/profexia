from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    room_id: str
    sender_id: int
    content: str
    created_at: datetime


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    recipient_id: int
    type: str
    title: str
    message: str
    data: dict | None
    is_read: bool
    created_at: datetime


class NotificationCreate(BaseModel):
    recipient_id: int
    type: str = Field(min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)
    data: dict | None = None


class LearningSessionCreate(BaseModel):
    course_id: int
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    scheduled_at: datetime | None = None


class LearningSessionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    scheduled_at: datetime | None = None
    status: str | None = Field(default=None, pattern="^(scheduled|live|ended|cancelled)$")


class LearningSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    course_id: int
    host_id: int
    title: str
    description: str | None
    scheduled_at: datetime | None
    status: str
    created_at: datetime
