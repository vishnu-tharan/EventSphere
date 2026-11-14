"""Database setup. SQLite locally; PostgreSQL with DATABASE_URL on Render."""
import os
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def normalize_url(url: str) -> str:
    if url.startswith('postgres://'):
        return 'postgresql+psycopg://' + url[len('postgres://'):]
    if url.startswith('postgresql://'):
        return 'postgresql+psycopg://' + url[len('postgresql://'):]
    return url


DATABASE_URL = normalize_url(os.getenv('DATABASE_URL', 'sqlite:///./eventsphere.db'))
engine = create_engine(
    DATABASE_URL,
    connect_args={'check_same_thread': False, 'timeout': 30} if DATABASE_URL.startswith('sqlite:') else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def lock_row(db, model, row_id):
    """Serialize decisions on a row and read its latest committed values."""
    if db.get_bind().dialect.name == 'sqlite':
        # SQLite ignores FOR UPDATE. Acquire its write lock before reading.
        db.execute(update(model).where(model.id == row_id).values(id=model.id),
                   execution_options={'synchronize_session': False})
    return db.scalar(select(model).where(model.id == row_id).with_for_update(key_share=True)
                     .execution_options(populate_existing=True))


def get_db():
    with SessionLocal() as db:
        yield db
