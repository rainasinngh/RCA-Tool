from sqlalchemy import Column, Integer, String, DateTime, JSON, Enum
from datetime import datetime
import enum

from .database import Base


class RCAStatus(str, enum.Enum):
    pending = "pending"
    analyzing = "analyzing"
    complete = "complete"
    failed = "failed"


class AlertWindow(Base):
    __tablename__ = "alert_windows"

    id = Column(Integer, primary_key=True, index=True)
    window_start = Column(DateTime, nullable=False)
    window_end = Column(DateTime, nullable=False)   # window_start + 10 min
    status = Column(String, default="open")         # open → analyzing → complete
    alert_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, index=True)

    # from Prometheus
    alert_name = Column(String, nullable=False)
    instance = Column(String)
    severity = Column(String)
    status = Column(String)
    fingerprint = Column(String, unique=True)       # deduplication key

    # timing
    starts_at = Column(DateTime)
    received_at = Column(DateTime, default=datetime.utcnow)

    # grouping + RCA state
    window_id = Column(Integer, nullable=True)      # which AlertWindow
    group_id = Column(String, nullable=True)        # set by correlation engine
    rca_status = Column(String, default="pending")

    raw_payload = Column(JSON)


class Evidence(Base):
    __tablename__ = "evidence"

    id = Column(Integer, primary_key=True, index=True)

    alert_id = Column(Integer, nullable=False, index=True)   # which Alert this evidence belongs to
    source = Column(String, nullable=False)                  # prometheus | deployments | k8s
    metric_name = Column(String, nullable=False)              # e.g. "node_load1" or "oomkill_events"

    raw_data = Column(JSON)      # full Prometheus/API response
    summary = Column(JSON)       # min/max/avg or counts — what RCA rules actually read

    collected_at = Column(DateTime, default=datetime.utcnow)


class AlertGroup(Base):
    __tablename__ = "alert_groups"

    id = Column(Integer, primary_key=True, index=True)

    window_id = Column(Integer, nullable=False, index=True)
    group_id = Column(String, unique=True, nullable=False)   # e.g. "window-3-group-1"

    alert_ids = Column(JSON, nullable=False)   # list[int] — alerts that belong to this group

    suspected_cause = Column(String)   # e.g. "recent_deployment", "isolated_incident"
    confidence = Column(String)        # high | medium | low
    signal = Column(String)            # human-readable reason the correlation engine grouped these

    created_at = Column(DateTime, default=datetime.utcnow)


class RCAFinding(Base):
    __tablename__ = "rca_findings"

    id = Column(Integer, primary_key=True, index=True)

    group_id = Column(String, nullable=False, index=True)
    window_id = Column(Integer, nullable=False, index=True)

    root_cause = Column(String, nullable=False)          # e.g. "oom_kill"
    root_cause_detail = Column(String)                    # human-readable explanation
    confidence = Column(String)                            # high | medium | low

    affected_hosts = Column(JSON)     # list[str]
    alert_types = Column(JSON)        # list[str]
    timeline = Column(JSON)           # list[dict] — chronological events
    suggested_actions = Column(JSON)  # list[str]

    created_at = Column(DateTime, default=datetime.utcnow)