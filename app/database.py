from collections.abc import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().db_url, connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Full-text index over food names, kept in sync with the foods table by triggers.
# unicode61 with diacritics removed matches "creme" to "crème"; Cyrillic works too.
FTS_SQL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS foods_fts USING fts5(
        name, name_ru, content='foods', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS foods_ai AFTER INSERT ON foods BEGIN
        INSERT INTO foods_fts(rowid, name, name_ru) VALUES (new.id, new.name, coalesce(new.name_ru, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS foods_ad AFTER DELETE ON foods BEGIN
        INSERT INTO foods_fts(foods_fts, rowid, name, name_ru)
        VALUES ('delete', old.id, old.name, coalesce(old.name_ru, ''));
    END""",
    """CREATE TRIGGER IF NOT EXISTS foods_au AFTER UPDATE ON foods BEGIN
        INSERT INTO foods_fts(foods_fts, rowid, name, name_ru)
        VALUES ('delete', old.id, old.name, coalesce(old.name_ru, ''));
        INSERT INTO foods_fts(rowid, name, name_ru) VALUES (new.id, new.name, coalesce(new.name_ru, ''));
    END""",
]


def init_db() -> None:
    from app import models  # noqa: F401  (registers tables on Base.metadata)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for stmt in FTS_SQL:
            conn.execute(text(stmt))


def get_session() -> Session:
    return SessionLocal()
