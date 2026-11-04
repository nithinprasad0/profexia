from pydantic import BaseModel, ConfigDict, Field

class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = None

class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: str | None = None

class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = None
    skill_ids: list[int] = Field(default_factory=list)

class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    skill_ids: list[int] | None = None

class TeacherResponse(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    bio: str | None = None

class CourseResponse(BaseModel):
    id: int
    title: str
    description: str | None
    teacher_id: int
    teacher_username: str
    skills: list[SkillResponse]

class EnrollmentResponse(BaseModel):
    id: int
    course_id: int
    learner_id: int
    progress_percent: int
    status: str

class ProgressUpdate(BaseModel):
    progress_percent: int = Field(ge=0, le=100)
