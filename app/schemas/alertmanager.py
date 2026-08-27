# app/schemas/alertmanager.py

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AlertmanagerAlert(BaseModel):
    status: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[str] = None
    endsAt: Optional[str] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerWebhookPayload(BaseModel):
    """
    Matches Alertmanager's webhook_config payload shape:
    https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
    """
    version: Optional[str] = None
    groupKey: Optional[str] = None
    status: Optional[str] = None
    receiver: Optional[str] = None
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = None
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)
