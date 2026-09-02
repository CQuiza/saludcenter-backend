"""Evaluaciones por módulo y progreso."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_current_user
from app.core.database import get_db
from app.models.enums import UserRole
from app.models.user import User
from app.repositories.course_repository import course_repository
from app.repositories.enrollment_repository import course_enrollment_repository
from app.repositories.module_assessment_repository import (
    module_assessment_repository,
)
from app.repositories.module_repository import module_repository
from app.repositories.user_assessment_repository import (
    user_assessment_repository,
)
from app.schemas.module_assessment import (
    AssessmentSubmit,
    AttemptResult,
    ModuleAssessmentCreate,
    ModuleAssessmentRead,
    ModuleAssessmentReadTeacher,
    AllProgressSummary,
    CourseProgressSummary,
)
from app.services.access import (
    is_student,
    is_super_or_admin,
    is_teacher,
    ensure_module_lesson_access,
    teacher_owns_module,
)
from app.schemas.module_assessment import AnswerResult

router = APIRouter(tags=["assessments"])


async def _get_assessment_or_404(
    db: AsyncSession, assessment_id: int
) -> object:
    assessment = await module_assessment_repository.get_by_id(db, assessment_id)
    if not assessment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evaluación no encontrada",
        )
    return assessment


async def _get_module_or_404(db: AsyncSession, module_id: int) -> object:
    mod = await module_repository.get_by_id(db, module_id)
    if not mod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Módulo no encontrado",
        )
    return mod


@router.get(
    "/modules/{module_id}/assessment",
    response_model=ModuleAssessmentRead | ModuleAssessmentReadTeacher,
)
async def get_module_assessment(
    module_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    mod = await _get_module_or_404(db, module_id)
    await ensure_module_lesson_access(
        db, current, module_id=module_id, need_student_enrollment=True
    )
    assessment = await module_assessment_repository.get_by_module(db, module_id)
    if not assessment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Este módulo no tiene evaluación",
        )

    if is_super_or_admin(current) or is_teacher(current):
        assessment_read = await module_assessment_repository.get_with_correct(
            db, assessment.id
        )
        return ModuleAssessmentReadTeacher.model_validate(assessment_read)
    else:
        assessment_read = await module_assessment_repository.get_student_view(
            db, assessment.id
        )
        return ModuleAssessmentRead.model_validate(assessment_read)


@router.post(
    "/modules/{module_id}/assessment",
    response_model=ModuleAssessmentReadTeacher,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_module_assessment(
    module_id: int,
    body: ModuleAssessmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    if not is_super_or_admin(current) and not is_teacher(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )
    mod = await _get_module_or_404(db, module_id)
    if is_teacher(current) and not await teacher_owns_module(db, current, mod):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No es su curso"
        )

    existing = await module_assessment_repository.get_by_module(db, module_id)
    if existing:
        await module_assessment_repository.delete(db, existing)
        await db.flush()

    questions_data = [q.model_dump() for q in body.questions]
    assessment = await module_assessment_repository.create_with_questions(
        db,
        module_id=module_id,
        passing_score=body.passing_score,
        questions_data=questions_data,
    )
    return ModuleAssessmentReadTeacher.model_validate(assessment)


@router.delete(
    "/assessments/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_assessment(
    assessment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> None:
    if not is_super_or_admin(current) and not is_teacher(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )
    assessment = await _get_assessment_or_404(db, assessment_id)
    if is_teacher(current):
        mod = await module_repository.get_by_id(db, assessment.module_id)
        if not mod or not await teacher_owns_module(db, current, mod):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="No es su curso"
            )
    await module_assessment_repository.delete(db, assessment)


@router.post(
    "/assessments/{assessment_id}/submit",
    response_model=AttemptResult,
)
async def submit_assessment(
    assessment_id: int,
    body: AssessmentSubmit,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> object:
    assessment = await _get_assessment_or_404(db, assessment_id)

    mod = await module_repository.get_by_id(db, assessment.module_id)
    if not mod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Módulo no encontrado"
        )

    if is_student(current):
        enrollment = await course_enrollment_repository.get_by_user_course(
            db, current.id, mod.course_id
        )
        if not enrollment:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No inscrito en este curso",
            )
    elif not is_super_or_admin(current) and not is_teacher(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )

    attempt = await user_assessment_repository.create_attempt(
        db, assessment_id=assessment_id, user_id=current.id
    )
    answers_data = [a.model_dump() for a in body.answers]
    result = await user_assessment_repository.submit_answers(
        db, attempt, answers_data
    )
    return result


@router.get(
    "/assessments/{assessment_id}/attempts",
    response_model=list[AttemptResult],
)
async def get_assessment_attempts(
    assessment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
) -> list:
    assessment = await _get_assessment_or_404(db, assessment_id)

    mod = await module_repository.get_by_id(db, assessment.module_id)
    if not mod:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Módulo no encontrado"
        )

    if is_super_or_admin(current) or is_teacher(current):
        pass
    elif is_student(current):
        enrollment = await course_enrollment_repository.get_by_user_course(
            db, current.id, mod.course_id
        )
        if not enrollment:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No inscrito en este curso",
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )

    attempts = await user_assessment_repository.get_attempts_by_assessment(
        db, assessment_id, current.id
    )
    # assessment ya trae preguntas + opciones (get_by_id); resuelve textos aquí
    options_by_question = {
        q.id: q.options
        for q in (assessment.questions if hasattr(assessment, "questions") else [])
    }
    results = []
    for a in attempts:
        answers = []
        for ans in a.answers:
            q_opts = options_by_question.get(ans.question_id, [])
            selected_opt = next(
                (o for o in q_opts if o.id == ans.selected_option_id), None
            )
            correct_opt = next((o for o in q_opts if o.is_correct), None)
            answers.append(
                AnswerResult(
                    question_id=ans.question_id,
                    question_text=ans.question.question_text if ans.question else "",
                    selected_option_id=ans.selected_option_id,
                    is_correct=ans.is_correct,
                    correct_option_id=correct_opt.id if correct_opt else None,
                    selected_option_text=selected_opt.option_text if selected_opt else None,
                    correct_option_text=correct_opt.option_text if correct_opt else None,
                )
            )
        total = sum(
            (q.points for q in (assessment.questions if hasattr(assessment, 'questions') else [])),
            0,
        )
        results.append(
            AttemptResult(
                attempt_id=a.id,
                score=float(a.score),
                passed=a.passed,
                total_points=total,
                earned_points=sum(1 for ans in a.answers if ans.is_correct),
                answers=answers,
            )
        )
    return results


@router.get(
    "/user-progress/summary",
    response_model=AllProgressSummary,
)
async def get_all_progress_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
    user_id: Annotated[int | None, Query()] = None,
) -> object:
    target_user_id = current.id

    if user_id is not None and user_id != current.id and not (
        is_super_or_admin(current) or is_teacher(current)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )
    if user_id is not None:
        target_user_id = user_id

    courses, overall = await user_assessment_repository.get_all_progress(
        db, target_user_id
    )
    return AllProgressSummary(courses=courses, overall_percent=overall)


@router.get(
    "/user-progress/summary/{course_id}",
    response_model=CourseProgressSummary,
)
async def get_course_progress_summary(
    course_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current: Annotated[User, Depends(get_current_user)],
    user_id: Annotated[int | None, Query()] = None,
) -> object:
    target_user_id = current.id

    if user_id is not None and user_id != current.id and not (
        is_super_or_admin(current) or is_teacher(current)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Sin permiso"
        )
    if user_id is not None:
        target_user_id = user_id

    course = await course_repository.get_by_id(db, course_id)
    if not course:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Curso no encontrado"
        )

    try:
        summary = await user_assessment_repository.get_course_progress(
            db, target_user_id, course_id
        )
        return summary
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        )
