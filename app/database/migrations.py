"""Versioned, additive migrations. Invoked inside init_db's transaction."""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, select, text, update


VERSION = "001_homework_management"


def migrate_homework(connection):
    from app.database.models import Homework, HomeworkAssignment, HomeworkSubmission

    connection.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL)"
    ))
    if connection.execute(text("SELECT version FROM schema_migrations WHERE version = :v"), {"v": VERSION}).first():
        return
    additions = {
        "homework": {"deadline": "TIMESTAMP WITH TIME ZONE"},
        "homework_submissions": {
            "answer": "TEXT", "status": "VARCHAR(20) NOT NULL DEFAULT 'not_submitted'",
            "revision": "INTEGER NOT NULL DEFAULT 0", "submitted_at": "TIMESTAMP WITH TIME ZONE",
            "feedback": "TEXT", "reviewed_at": "TIMESTAMP WITH TIME ZONE", "reviewed_by": "BIGINT",
        },
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in inspect(connection).get_columns(table)}
        for name, declaration in columns.items():
            if name not in existing:
                # Names and declarations are fixed migration code, never user input.
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"))

    # Preserve historical completion, without inventing answers or review metadata.
    connection.execute(update(HomeworkSubmission).where(
        HomeworkSubmission.completed.is_(True), HomeworkSubmission.status == "not_submitted",
    ).values(status="approved"))
    for row in connection.execute(select(Homework.id, Homework.due_date).where(
        Homework.deadline.is_(None), Homework.due_date.is_not(None),
    )).all():
        deadline = datetime.combine(row.due_date, time(23, 59, 59), ZoneInfo("Asia/Almaty"))
        connection.execute(update(Homework).where(Homework.id == row.id).values(deadline=deadline.astimezone(timezone.utc)))
    # Only records already tied to a student become historical assignments.
    assignments = connection.execute(select(
        HomeworkSubmission.student_id, HomeworkSubmission.homework_id,
    )).all()
    for student_id, homework_id in assignments:
        exists = connection.execute(select(HomeworkAssignment.student_id).where(
            HomeworkAssignment.student_id == student_id, HomeworkAssignment.homework_id == homework_id,
        )).first()
        if not exists:
            connection.execute(HomeworkAssignment.__table__.insert().values(
                student_id=student_id, homework_id=homework_id,
                assigned_at=datetime.now(timezone.utc), reminder_queued=False,
            ))
    connection.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": VERSION})
