from fastapi import FastAPI
from pydantic import BaseModel

from app.database import SessionLocal
from app.repositories.candidate import get_or_create_candidate
from app.repositories.conversation import save_message


app = FastAPI(
    title="Flow",
    version="0.1.0",
)


class IncomingMessage(BaseModel):
    phone_number: str
    message: str


@app.post("/webhook")
def receive_message(payload: IncomingMessage):

    with SessionLocal() as db:

        candidate, created = get_or_create_candidate(
            db,
            payload.phone_number,
        )

        save_message(
            db,
            candidate.id,
            payload.message,
            "incoming",
        )

    return {
        "success": True,
        "candidate_id": candidate.id,
        "new_candidate": created,
        "phone_number": candidate.phone_number,
        "message": payload.message,
    }
