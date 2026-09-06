from sqlalchemy import text

from app.database import engine


def test_database_connection():
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database()")
        )

        assert result.scalar() == "flow"