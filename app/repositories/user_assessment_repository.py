"""Repositorio de intentos de evaluación y progreso."""

from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.assessment_option import AssessmentOption
from app.models.assessment_question import AssessmentQuestion
from app.models.course import Course
from app.models.lesson import Lesson
from app.models.lesson_task import LessonTask
from app.models.module import Module
from app.models.module_assessment import ModuleAssessment
from app.models.task_submission import TaskSubmission
from app.models.user_assessment_attempt import (
    UserAssessmentAnswer,
    UserAssessmentAttempt,
)
from app.schemas.module_assessment import (
    AnswerResult,
    AttemptResult,
    CourseProgressSummary,
    ModuleProgressItem,
    TaskProgressItem,
)


class UserAssessmentRepository:
    async def create_attempt(
        self,
        db: AsyncSession,
        *,
        assessment_id: int,
        user_id: int,
    ) -> UserAssessmentAttempt:
        attempt = UserAssessmentAttempt(
            assessment_id=assessment_id,
            user_id=user_id,
            started_at=datetime.now(timezone.utc),
        )
        db.add(attempt)
        await db.flush()
        await db.refresh(attempt)
        return attempt

    async def submit_answers(
        self,
        db: AsyncSession,
        attempt: UserAssessmentAttempt,
        answers: list[dict],
    ) -> AttemptResult:
        assessment = await db.execute(
            select(ModuleAssessment)
            .where(ModuleAssessment.id == attempt.assessment_id)
            .options(
                selectinload(ModuleAssessment.questions).selectinload(
                    AssessmentQuestion.options
                )
            )
        )
        assessment = assessment.scalar_one_or_none()
        if not assessment:
            raise ValueError("Assessment no encontrado")

        questions_map = {q.id: q for q in assessment.questions}
        options_map: dict[int, AssessmentOption] = {}
        for q in assessment.questions:
            for opt in q.options:
                options_map[opt.id] = opt

        total_points = sum(q.points for q in assessment.questions)
        earned_points = 0
        answer_results: list[AnswerResult] = []

        for ans in answers:
            qid = ans["question_id"]
            optid = ans["selected_option_id"]

            question = questions_map.get(qid)
            if not question:
                raise ValueError(f"Pregunta {qid} no encontrada")

            option = options_map.get(optid)
            if not option or option.question_id != qid:
                raise ValueError(
                    f"Opción {optid} no válida para pregunta {qid}"
                )

            is_correct = option.is_correct
            if is_correct:
                earned_points += question.points

            answer_obj = UserAssessmentAnswer(
                attempt_id=attempt.id,
                question_id=qid,
                selected_option_id=optid,
                is_correct=is_correct,
            )
            db.add(answer_obj)

            correct_opt = next(
                (o for o in question.options if o.is_correct), None
            )

            answer_results.append(
                AnswerResult(
                    question_id=qid,
                    question_text=question.question_text,
                    selected_option_id=optid,
                    is_correct=is_correct,
                    correct_option_id=correct_opt.id if correct_opt else None,
                    selected_option_text=option.option_text,
                    correct_option_text=correct_opt.option_text if correct_opt else None,
                )
            )

        score = (
            Decimal(0)
            if total_points == 0
            else Decimal(str(round((earned_points / total_points) * 100, 2)))
        )
        passed = score >= Decimal(str(assessment.passing_score))

        attempt.score = score
        attempt.passed = passed
        attempt.finished_at = datetime.now(timezone.utc)

        await db.flush()
        await db.refresh(attempt)

        return AttemptResult(
            attempt_id=attempt.id,
            score=float(score),
            passed=passed,
            total_points=total_points,
            earned_points=earned_points,
            answers=answer_results,
        )

    async def get_attempt(
        self, db: AsyncSession, attempt_id: int
    ) -> UserAssessmentAttempt | None:
        r = await db.execute(
            select(UserAssessmentAttempt)
            .where(UserAssessmentAttempt.id == attempt_id)
            .options(
                selectinload(UserAssessmentAttempt.answers).selectinload(
                    UserAssessmentAnswer.question
                )
            )
        )
        return r.scalar_one_or_none()

    async def get_attempts_by_assessment(
        self,
        db: AsyncSession,
        assessment_id: int,
        user_id: int,
    ) -> Sequence[UserAssessmentAttempt]:
        r = await db.execute(
            select(UserAssessmentAttempt)
            .where(
                UserAssessmentAttempt.assessment_id == assessment_id,
                UserAssessmentAttempt.user_id == user_id,
            )
            .order_by(UserAssessmentAttempt.started_at.desc())
        )
        return r.scalars().all()

    async def has_passed(
        self,
        db: AsyncSession,
        assessment_id: int,
        user_id: int,
    ) -> bool:
        r = await db.execute(
            select(UserAssessmentAttempt).where(
                UserAssessmentAttempt.assessment_id == assessment_id,
                UserAssessmentAttempt.user_id == user_id,
                UserAssessmentAttempt.passed == True,
            )
        )
        return r.scalar_one_or_none() is not None

    async def get_course_progress(
        self,
        db: AsyncSession,
        user_id: int,
        course_id: int,
    ) -> CourseProgressSummary:
        course_r = await db.execute(
            select(Course)
            .where(Course.id == course_id)
            .options(
                selectinload(Course.modules)
                .selectinload(Module.assessment)
                .selectinload(ModuleAssessment.questions),
            )
        )
        course = course_r.scalar_one_or_none()
        if not course:
            raise ValueError("Curso no encontrado")

        modules = course.modules
        task_progress_map = await self._get_module_task_progress(
            db, modules, user_id
        )

        assessment_ids = [m.assessment.id for m in modules if m.assessment]
        attempts_map: dict[int, list[UserAssessmentAttempt]] = {}
        if assessment_ids:
            a_r = await db.execute(
                select(UserAssessmentAttempt)
                .where(
                    UserAssessmentAttempt.assessment_id.in_(assessment_ids),
                    UserAssessmentAttempt.user_id == user_id,
                )
                .order_by(UserAssessmentAttempt.finished_at.desc().nullslast())
            )
            for a in a_r.scalars().all():
                attempts_map.setdefault(a.assessment_id, []).append(a)

        module_items: list[ModuleProgressItem] = []
        completed = 0

        for mod in modules:
            assessment = mod.assessment
            total_q = len(assessment.questions) if assessment else 0
            attempts = attempts_map.get(assessment.id, []) if assessment else []
            attempts_count = len(attempts)
            last_score = float(attempts[0].score) if attempts else None
            passed = any(a.passed for a in attempts)

            if passed:
                completed += 1

            tp = task_progress_map.get(mod.id, {"total_tasks": 0, "submitted_tasks": 0, "tasks": []})

            module_items.append(
                ModuleProgressItem(
                    module_id=mod.id,
                    module_title=mod.title,
                    module_order=mod.order_index,
                    total_assessment_questions=total_q,
                    attempts_count=attempts_count,
                    last_score=last_score,
                    passed=passed,
                    total_tasks=tp["total_tasks"],
                    submitted_tasks=tp["submitted_tasks"],
                    tasks=[TaskProgressItem(**t) for t in tp["tasks"]],
                )
            )

        total_modules = len(modules)
        progress_pct = (
            round((completed / total_modules) * 100, 2)
            if total_modules > 0
            else 0.0
        )

        return CourseProgressSummary(
            course_id=course.id,
            course_title=course.title,
            total_modules=total_modules,
            completed_modules=completed,
            progress_percent=progress_pct,
            modules=module_items,
        )

    async def get_all_progress(
        self,
        db: AsyncSession,
        user_id: int,
    ) -> tuple[list[CourseProgressSummary], float]:
        from app.models.course import CourseEnrollment

        enr_r = await db.execute(
            select(CourseEnrollment.course_id).where(
                CourseEnrollment.user_id == user_id
            )
        )
        enrolled_ids = {row[0] for row in enr_r.all()}
        if not enrolled_ids:
            return [], 0.0

        r = await db.execute(
            select(Course)
            .where(Course.id.in_(enrolled_ids))
            .order_by(Course.title)
            .options(
                selectinload(Course.modules)
                .selectinload(Module.assessment)
                .selectinload(ModuleAssessment.questions),
            )
        )
        courses = r.scalars().all()

        all_modules = [m for c in courses for m in c.modules]
        task_progress_map = await self._get_module_task_progress(
            db, all_modules, user_id
        )

        all_assessment_ids = [
            m.assessment.id for c in courses
            for m in c.modules if m.assessment
        ]
        attempts_map: dict[int, list[UserAssessmentAttempt]] = {}
        if all_assessment_ids:
            a_r = await db.execute(
                select(UserAssessmentAttempt)
                .where(
                    UserAssessmentAttempt.assessment_id.in_(all_assessment_ids),
                    UserAssessmentAttempt.user_id == user_id,
                )
                .order_by(UserAssessmentAttempt.finished_at.desc())
            )
            for a in a_r.scalars().all():
                attempts_map.setdefault(a.assessment_id, []).append(a)

        course_summaries: list[CourseProgressSummary] = []
        for course in courses:
            modules = course.modules
            module_items: list[ModuleProgressItem] = []
            completed = 0

            for mod in modules:
                assessment = mod.assessment
                total_q = len(assessment.questions) if assessment else 0
                attempts = attempts_map.get(assessment.id, []) if assessment else []
                attempts_count = len(attempts)
                last_score = float(attempts[0].score) if attempts else None
                passed = any(a.passed for a in attempts)

                if passed:
                    completed += 1

                tp = task_progress_map.get(mod.id, {"total_tasks": 0, "submitted_tasks": 0, "tasks": []})

                module_items.append(
                    ModuleProgressItem(
                        module_id=mod.id,
                        module_title=mod.title,
                        module_order=mod.order_index,
                        total_assessment_questions=total_q,
                        attempts_count=attempts_count,
                        last_score=last_score,
                        passed=passed,
                        total_tasks=tp["total_tasks"],
                        submitted_tasks=tp["submitted_tasks"],
                        tasks=[TaskProgressItem(**t) for t in tp["tasks"]],
                    )
                )

            total = len(modules)
            pct = round((completed / total) * 100, 2) if total > 0 else 0.0

            course_summaries.append(
                CourseProgressSummary(
                    course_id=course.id,
                    course_title=course.title,
                    total_modules=total,
                    completed_modules=completed,
                    progress_percent=pct,
                    modules=module_items,
                )
            )

        overall = round(
            sum(s.progress_percent for s in course_summaries) / len(course_summaries), 2
        ) if course_summaries else 0.0

        return course_summaries, overall


    async def _get_module_task_progress(
        self,
        db: AsyncSession,
        modules: list[Module],
        user_id: int,
    ) -> dict[int, dict]:
        """Retorna mapa module_id → {total_tasks, submitted_tasks, tasks[]}."""
        module_ids = [m.id for m in modules]
        if not module_ids:
            return {}

        lessons_r = await db.execute(
            select(Lesson).where(Lesson.module_id.in_(module_ids))
        )
        lessons = lessons_r.scalars().all()
        if not lessons:
            return {m.id: {"total_tasks": 0, "submitted_tasks": 0, "tasks": []} for m in modules}

        lesson_ids = [l.id for l in lessons]
        lesson_map: dict[int, list[int]] = {}
        for l in lessons:
            lesson_map.setdefault(l.module_id, []).append(l.id)

        tasks_r = await db.execute(
            select(LessonTask).where(LessonTask.lesson_id.in_(lesson_ids))
        )
        task_list = tasks_r.scalars().all()
        if not task_list:
            return {m.id: {"total_tasks": 0, "submitted_tasks": 0, "tasks": []} for m in modules}

        task_ids = [t.id for t in task_list]
        task_by_id = {t.id: t for t in task_list}
        task_by_lesson: dict[int, list[LessonTask]] = {}
        for t in task_list:
            task_by_lesson.setdefault(t.lesson_id, []).append(t)

        subs_r = await db.execute(
            select(TaskSubmission).where(
                TaskSubmission.task_id.in_(task_ids),
                TaskSubmission.user_id == user_id,
            )
        )
        submissions = subs_r.scalars().all()
        submitted_task_ids = {s.task_id for s in submissions}
        sub_by_task = {s.task_id: s for s in submissions}

        result: dict[int, dict] = {}
        for mod in modules:
            mod_lesson_ids = lesson_map.get(mod.id, [])
            mod_tasks: list[LessonTask] = []
            for lid in mod_lesson_ids:
                mod_tasks.extend(task_by_lesson.get(lid, []))

            tasks_detail = []
            submitted_count = 0
            for t in mod_tasks:
                is_submitted = t.id in submitted_task_ids
                if is_submitted:
                    submitted_count += 1
                sub = sub_by_task.get(t.id)
                tasks_detail.append(
                    {
                        "task_id": t.id,
                        "task_title": t.title,
                        "submitted": is_submitted,
                        "submission_id": sub.id if sub else None,
                        "file_url": sub.file_url if sub else None,
                        "original_filename": sub.original_filename if sub else None,
                        "submitted_at": sub.submitted_at if sub else None,
                    }
                )

            result[mod.id] = {
                "total_tasks": len(mod_tasks),
                "submitted_tasks": submitted_count,
                "tasks": tasks_detail,
            }

        return result


user_assessment_repository = UserAssessmentRepository()
