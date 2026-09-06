from app.database import SessionLocal
from app.repositories.candidate import get_candidate_by_phone
from app.repositories.conversation import (
    get_recent_messages,
)


def get_conversation_history(
    phone_number: str,
    limit: int = 20,
) -> dict:
    """
    Retrieve recent conversation history for a candidate.
    """

    with SessionLocal() as db:

        candidate = get_candidate_by_phone(
            db,
            phone_number,
        )

        if candidate is None:
            return {
                "exists": False,
                "messages": [],
            }

        messages = get_recent_messages(
            db,
            candidate.id,
            limit,
        )

        return {
            "exists": True,
            "messages": [
                {
                    "direction": message.direction,
                    "message": message.message,
                    "created_at": (
                        message.created_at.isoformat()
                        if message.created_at
                        else None
                    ),
                }
                for message in messages
            ],
        }
