from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def make_engine(database_url: str) -> Engine:
    kwargs = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, pool_pre_ping=True, connect_args=kwargs)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def initialize_database(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    columns = {column["name"] for column in inspect(engine).get_columns("job_match_scores")}
    additions = {
        "role_family": "VARCHAR(100) DEFAULT 'Other'",
        "primary_language": "VARCHAR(50) DEFAULT 'unknown'",
        "eligibility_status": "VARCHAR(50) DEFAULT 'needs_review'",
        "resume_relevance": "FLOAT DEFAULT 0",
        "application_eligibility": "FLOAT DEFAULT 0",
        "posting_freshness": "FLOAT DEFAULT 0",
        "application_priority": "FLOAT DEFAULT 0",
        "details": "JSON",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE job_match_scores ADD COLUMN {name} {definition}")
                )
