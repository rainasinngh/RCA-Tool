from fastapi import FastAPI
from sqlalchemy.orm import Session

from .database import engine, SessionLocal
from .models import Base, Alert

Base.metadata.create_all(bind=engine)

app = FastAPI()


@app.get("/")
async def root():
    return {"status": "RCA Engine Running"}


@app.post("/webhook/alertmanager")
async def receive_alert(payload: dict):

    db: Session = SessionLocal()

    try:

        alerts = payload.get("alerts", [])

        for item in alerts:

            labels = item.get("labels", {})

            alert = Alert(
                alert_name=labels.get("alertname"),
                instance=labels.get("instance"),
                severity=labels.get("severity"),
                status=item.get("status"),
                starts_at=item.get("startsAt"),
                raw_payload=item
            )

            db.add(alert)

        db.commit()

        return {
            "status": "success",
            "alerts_received": len(alerts)
        }

    finally:
        db.close()