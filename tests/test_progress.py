"""Pruebas del cálculo de progreso de cursos.

El progreso se calcula por módulo: un módulo cuenta como completo si cumple
todos los requisitos que tenga (evaluación aprobada y tareas entregadas). Los
módulos sin evaluación o sin tareas no deben bloquear el avance.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401  (registra todos los mappers)
from app.core.database import Base
from app.models import (
    AssessmentOption,
    AssessmentQuestion,
    CertificateType,
    Course,
    CourseEnrollment,
    Lesson,
    LessonTask,
    Module,
    ModuleAssessment,
    TaskSubmission,
    User,
    UserAssessmentAttempt,
)
from app.repositories.user_assessment_repository import (
    _is_module_completed,
    user_assessment_repository,
)

# Solo las tablas necesarias. Se excluyen las que usan tipos exclusivos de
# PostgreSQL (JSONB) para poder ejecutar las pruebas sobre SQLite.
TABLES = [
    User.__table__,
    CertificateType.__table__,
    Course.__table__,
    CourseEnrollment.__table__,
    Module.__table__,
    Lesson.__table__,
    LessonTask.__table__,
    ModuleAssessment.__table__,
    AssessmentQuestion.__table__,
    AssessmentOption.__table__,
    UserAssessmentAttempt.__table__,
    TaskSubmission.__table__,
]


# ---------------------------------------------------------------------------
# Regla pura
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "has_assessment",
        "passed",
        "total_tasks",
        "submitted_tasks",
        "expected",
    ),
    [
        # Sin evaluación ni tareas: siempre completo.
        (False, False, 0, 0, True),
        # Evaluación aprobada / no aprobada.
        (True, True, 0, 0, True),
        (True, False, 0, 0, False),
        # Tareas todas entregadas / pendientes.
        (False, False, 2, 2, True),
        (False, False, 2, 1, False),
        # Ambos requisitos.
        (True, True, 2, 2, True),
        (True, True, 2, 1, False),
        (True, False, 2, 2, False),
    ],
)
def test_is_module_completed(
    has_assessment, passed, total_tasks, submitted_tasks, expected
):
    assert (
        _is_module_completed(
            has_assessment=has_assessment,
            passed=passed,
            total_tasks=total_tasks,
            submitted_tasks=submitted_tasks,
        )
        is expected
    )


# ---------------------------------------------------------------------------
# Escenarios contra base de datos
# ---------------------------------------------------------------------------


async def _create_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=TABLES)
        )
    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    return engine, session_factory


async def _seed_course(
    session: AsyncSession,
    *,
    module_count: int,
    assessed: set[int],
    failed: set[int] | None = None,
    with_tasks: set[int] | None = None,
    submitted: bool = False,
) -> tuple[User, Course]:
    """Crea un curso con módulos, evaluaciones y tareas opcionales."""
    failed = failed or set()
    with_tasks = with_tasks or set()

    student = User(
        email="student@example.com",
        password_hash="x",
        name="Estudiante",
        first_last_name="Prueba",
        role="student",
        identity_type="CC",
        identity_number="1234567890",
        phone_number="+570000000000",
    )
    session.add(student)
    await session.flush()

    course = Course(title="Curso de prueba", status="published")
    session.add(course)
    await session.flush()

    session.add(CourseEnrollment(user_id=student.id, course_id=course.id))

    for index in range(module_count):
        module = Module(
            course_id=course.id,
            title=f"Módulo {index + 1}",
            order_index=index,
        )
        session.add(module)
        await session.flush()

        if index in assessed:
            assessment = ModuleAssessment(module_id=module.id, passing_score=70)
            session.add(assessment)
            await session.flush()

            question = AssessmentQuestion(
                assessment_id=assessment.id,
                question_text="¿Pregunta?",
                question_type="true_false",
                points=1,
                order_index=0,
            )
            session.add(question)
            await session.flush()
            session.add_all(
                [
                    AssessmentOption(
                        question_id=question.id,
                        option_text="Verdadero",
                        is_correct=True,
                    ),
                    AssessmentOption(
                        question_id=question.id,
                        option_text="Falso",
                        is_correct=False,
                    ),
                ]
            )

            session.add(
                UserAssessmentAttempt(
                    assessment_id=assessment.id,
                    user_id=student.id,
                    score=100 if index not in failed else 40,
                    passed=index not in failed,
                    finished_at=datetime.now(timezone.utc),
                )
            )

        if index in with_tasks:
            lesson = Lesson(
                module_id=module.id,
                title=f"Lección {index + 1}",
                order_index=0,
            )
            session.add(lesson)
            await session.flush()

            task = LessonTask(
                lesson_id=lesson.id,
                title=f"Tarea {index + 1}",
                order_index=0,
            )
            session.add(task)
            await session.flush()

            if submitted:
                session.add(
                    TaskSubmission(
                        task_id=task.id,
                        user_id=student.id,
                        file_url="http://example.com/file.pdf",
                        original_filename="file.pdf",
                    )
                )

    await session.commit()
    return student, course


async def _run_scenario(**kwargs) -> tuple[User, Course, object]:
    engine, session_factory = await _create_database()
    try:
        async with session_factory() as session:
            student, course = await _seed_course(session, **kwargs)
            summary = await user_assessment_repository.get_course_progress(
                session, student.id, course.id
            )
            return student, course, summary
    finally:
        await engine.dispose()


def test_curso_de_10_modulos_con_2_evaluaciones_llega_a_100():
    student, course, summary = asyncio.run(
        _run_scenario(module_count=10, assessed={0, 1})
    )

    assert summary.total_modules == 10
    assert summary.completed_modules == 10
    assert summary.progress_percent == 100.0
    assert all(mod.completed for mod in summary.modules)


def test_curso_de_12_modulos_con_5_evaluaciones_llega_a_100():
    student, course, summary = asyncio.run(
        _run_scenario(module_count=12, assessed={0, 2, 4, 6, 8})
    )

    assert summary.total_modules == 12
    assert summary.completed_modules == 12
    assert summary.progress_percent == 100.0


def test_evaluacion_no_aprobada_impide_el_100():
    student, course, summary = asyncio.run(
        _run_scenario(module_count=10, assessed={0, 1}, failed={1})
    )

    assert summary.completed_modules == 9
    assert summary.progress_percent == 90.0
    assert summary.modules[1].completed is False


def test_tarea_no_entregada_impide_el_100():
    student, course, summary = asyncio.run(
        _run_scenario(
            module_count=3,
            assessed={0},
            with_tasks={2},
            submitted=False,
        )
    )

    assert summary.completed_modules == 2
    assert summary.progress_percent == pytest.approx(66.67, abs=0.01)
    assert summary.modules[2].completed is False
    assert summary.modules[2].submitted_tasks == 0
    assert summary.modules[2].total_tasks == 1


def test_tareas_entregadas_permiten_el_100():
    student, course, summary = asyncio.run(
        _run_scenario(
            module_count=3,
            assessed={0},
            with_tasks={2},
            submitted=True,
        )
    )

    assert summary.completed_modules == 3
    assert summary.progress_percent == 100.0
    assert summary.modules[2].completed is True


def test_progreso_global_usa_la_misma_regla():
    async def scenario():
        engine, session_factory = await _create_database()
        try:
            async with session_factory() as session:
                student, course = await _seed_course(
                    session, module_count=10, assessed={0, 1}
                )
                courses, overall = (
                    await user_assessment_repository.get_all_progress(
                        session, student.id
                    )
                )
                return courses, overall
        finally:
            await engine.dispose()

    courses, overall = asyncio.run(scenario())

    assert len(courses) == 1
    assert courses[0].completed_modules == 10
    assert courses[0].progress_percent == 100.0
    assert overall == 100.0


def test_evaluacion_sin_preguntas_no_cuenta_como_completa():
    """Una evaluación existente pero vacía no debe completar el módulo."""

    async def scenario():
        engine, session_factory = await _create_database()
        try:
            async with session_factory() as session:
                student = User(
                    email="vacio@example.com",
                    password_hash="x",
                    name="Estudiante",
                    first_last_name="Prueba",
                    role="student",
                    identity_type="CC",
                    identity_number="0987654321",
                    phone_number="+570000000001",
                )
                session.add(student)
                await session.flush()

                course = Course(title="Curso vacío", status="published")
                session.add(course)
                await session.flush()

                module = Module(
                    course_id=course.id, title="Módulo 1", order_index=0
                )
                session.add(module)
                await session.flush()

                session.add(
                    ModuleAssessment(module_id=module.id, passing_score=70)
                )
                await session.commit()

                return await user_assessment_repository.get_course_progress(
                    session, student.id, course.id
                )
        finally:
            await engine.dispose()

    summary = asyncio.run(scenario())

    assert summary.total_modules == 1
    assert summary.completed_modules == 0
    assert summary.progress_percent == 0.0
    assert summary.modules[0].total_assessment_questions == 0
    assert summary.modules[0].completed is False
