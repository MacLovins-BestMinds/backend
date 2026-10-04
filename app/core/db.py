import logging
from typing import Generator
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy import inspect, text
from app.core.config import settings

logger = logging.getLogger(__name__)


def create_db_engine():
    db_url = settings.DATABASE_URL
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    if db_url.startswith("sqlite"):
        return create_engine(
            db_url,
            echo=False,
            connect_args={"check_same_thread": False},
        )

    # Попытка подключения к PostgreSQL
    try:
        pg_engine = create_engine(
            db_url,
            echo=False,
            pool_pre_ping=True,
            pool_size=10,
            max_overflow=20,
        )
        # Быстрая проверка доступности сервера Postgres
        with pg_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info(f"✓ Успешное подключение к PostgreSQL: {db_url.split('@')[-1] if '@' in db_url else db_url}")
        return pg_engine
    except Exception as e:
        if not settings.DATABASE_SQLITE_FALLBACK:
            raise RuntimeError(f"PostgreSQL недоступен ({e}); DATABASE_SQLITE_FALLBACK=false") from e
        logger.warning(
            f"Не удалось подключиться к PostgreSQL ({e}). "
            f"Переключаемся на локальный SQLite fallback (sqlite:///stage_zero.db)."
        )
        return create_engine(
            "sqlite:///stage_zero.db",
            echo=False,
            connect_args={"check_same_thread": False},
        )


engine = create_db_engine()


# Колонки, добавленные после первого релиза: create_all их в существующую таблицу не дописывает.
_ADDED_COLUMNS = {
    "cases": {"level": "VARCHAR NOT NULL DEFAULT 'easy'"},
    "rounds": {"difficulty": "VARCHAR NOT NULL DEFAULT 'easy'", "lang": "VARCHAR NOT NULL DEFAULT 'en'"},
    "users": {
        "email_verified": "BOOLEAN NOT NULL DEFAULT FALSE",
        "verify_code": "VARCHAR",
        "verify_expires": "TIMESTAMP",
    },
}


def _add_missing_columns() -> None:
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            if not inspector.has_table(table):
                continue
            present = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    logger.info("БД: в таблицу %s добавлена колонка %s", table, name)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
