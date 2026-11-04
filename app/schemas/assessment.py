from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuestionCreate(BaseModel):
    text: str = Field(min_length=1)
    options: list[str] = Field(min_length=2, max_length=10)
    correct_option: int = Field(ge=0)
    explanation: str | None = None
    marks: int = Field(default=1, ge=1)
    is_active: bool = True

    @model_validator(mode="after")
    def validate_correct_option(self):
        if self.correct_option >= len(self.options):
            raise ValueError("correct_option must reference an existing option")
        return self


class QuestionUpdate(BaseModel):
    text: str | None = Field(default=None, min_length=1)
    options: list[str] | None = Field(default=None, min_length=2, max_length=10)
    correct_option: int | None = Field(default=None, ge=0)
    explanation: str | None = None
    marks: int | None = Field(default=None, ge=1)
    is_active: bool | None = None


class QuestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    course_id: int
    teacher_id: int
    text: str
    options: list[str]
    explanation: str | None
    marks: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TestCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    time_limit_minutes: int | None = Field(default=None, ge=1)
    question_ids: list[int] = Field(default_factory=list)
    is_published: bool = False


class TestUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    time_limit_minutes: int | None = Field(default=None, ge=1)
    question_ids: list[int] | None = None
    is_published: bool | None = None


class TestResponse(BaseModel):
    id: int
    course_id: int
    teacher_id: int
    title: str
    description: str | None
    time_limit_minutes: int | None
    max_marks: int
    is_published: bool
    question_ids: list[int]
    created_at: datetime
    updated_at: datetime


class TestQuestionResponse(BaseModel):
    id: int
    text: str
    options: list[str]
    marks: int


class AttemptResponse(BaseModel):
    id: int
    test_id: int
    learner_id: int
    started_at: datetime
    submitted_at: datetime | None
    status: str
    questions: list[TestQuestionResponse]


class AnswerInput(BaseModel):
    question_id: int
    selected_option: int | None = Field(default=None, ge=0)


class SubmitRequest(BaseModel):
    answers: list[AnswerInput]


class ResultAnswer(BaseModel):
    question_id: int
    selected_option: int | None
    correct: bool
    marks_awarded: int


class ResultResponse(BaseModel):
    id: int
    test_id: int
    learner_id: int
    submission_id: int
    score: int
    max_score: int
    percentage: float
    passed: bool
    submitted_at: datetime
    answers: list[ResultAnswer]
