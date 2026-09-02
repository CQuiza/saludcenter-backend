"""Esquemas de evaluaciones por módulo."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from typing import Literal


class AssessmentOptionCreate(BaseModel):
    option_text: str
    is_correct: bool = False


class AssessmentOptionRead(BaseModel):
    id: int
    option_text: str
    model_config = ConfigDict(from_attributes=True)


class AssessmentOptionReadWithCorrect(AssessmentOptionRead):
    is_correct: bool


class AssessmentQuestionCreate(BaseModel):
    question_text: str
    question_type: Literal["multiple_choice", "true_false"]
    points: int = 1
    order_index: int = 0
    options: list[AssessmentOptionCreate]

    @field_validator("options")
    @classmethod
    def validate_options(cls, v: list, info) -> list:
        qtype = info.data.get("question_type")
        if qtype == "true_false" and len(v) != 2:
            raise ValueError("true_false debe tener exactamente 2 opciones")
        if qtype == "multiple_choice" and (len(v) < 2 or len(v) > 4):
            raise ValueError("multiple_choice debe tener entre 2 y 4 opciones")
        return v


class AssessmentQuestionRead(BaseModel):
    id: int
    question_text: str
    question_type: str
    points: int
    order_index: int
    options: list[AssessmentOptionRead]
    model_config = ConfigDict(from_attributes=True)


class AssessmentQuestionReadWithCorrect(AssessmentQuestionRead):
    options: list[AssessmentOptionReadWithCorrect]


class ModuleAssessmentCreate(BaseModel):
    passing_score: int = 70
    questions: list[AssessmentQuestionCreate]


class ModuleAssessmentRead(BaseModel):
    id: int
    module_id: int
    passing_score: int
    questions: list[AssessmentQuestionRead]
    model_config = ConfigDict(from_attributes=True)


class ModuleAssessmentReadTeacher(ModuleAssessmentRead):
    questions: list[AssessmentQuestionReadWithCorrect]


class AnswerSubmission(BaseModel):
    question_id: int
    selected_option_id: int


class AssessmentSubmit(BaseModel):
    answers: list[AnswerSubmission]


class AnswerResult(BaseModel):
    question_id: int
    question_text: str
    selected_option_id: int
    is_correct: bool
    correct_option_id: int | None = None
    selected_option_text: str | None = None
    correct_option_text: str | None = None


class AttemptResult(BaseModel):
    attempt_id: int
    score: float
    passed: bool
    total_points: int
    earned_points: int
    answers: list[AnswerResult]





class TaskProgressItem(BaseModel):
    task_id: int
    task_title: str
    submitted: bool
    submission_id: int | None = None
    file_url: str | None = None
    original_filename: str | None = None
    submitted_at: datetime | None = None


class ModuleProgressItem(BaseModel):
    module_id: int
    module_title: str
    module_order: int
    total_assessment_questions: int
    attempts_count: int
    last_score: float | None
    passed: bool
    total_tasks: int = 0
    submitted_tasks: int = 0
    tasks: list[TaskProgressItem] = []


class CourseProgressSummary(BaseModel):
    course_id: int
    course_title: str
    total_modules: int
    completed_modules: int
    progress_percent: float
    modules: list[ModuleProgressItem]


class AllProgressSummary(BaseModel):
    courses: list[CourseProgressSummary]
    overall_percent: float
