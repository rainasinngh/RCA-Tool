from sqlalchemy import Column, Integer, String, DateTime, JSON
from datetime import datetime

from .database import Base


class Alert(Base):

    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)

    alert_name = Column(String)
    instance = Column(String)
    severity = Column(String)
    status = Column(String)

    starts_at = Column(DateTime)

    received_at = Column(
        DateTime,
        default=datetime.utcnow
    )

    raw_payload = Column(JSON)