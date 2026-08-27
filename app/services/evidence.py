# app/services/evidence.py

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .prometheus import PrometheusService
from ..metric_map import get_metric_config
from ..models import Evidence
from ..utils.promql import safe_label_value, UnsafeLabelValueError

logger = logging.getLogger(__name__)


class EvidenceCollector:

    def __init__(self):
        self.prometheus = PrometheusService()

    def collect_for_alert(
        self,
        db: Session,
        alert_id: int,
        alert_name: str,
        instance: str,
        alert_time: datetime,
    ) -> list:
        """
        Main entry point.
        Given one alert, collect all relevant evidence and save to DB.
        Returns list of Evidence objects created.
        """

        logger.info(f"Collecting evidence for alert {alert_id} "
                    f"({alert_name} on {instance})")

        # look up which metrics to collect for this alert type
        config = get_metric_config(alert_name)
        metrics = config["metrics"]
        filter_label = config["filter_label"]
        also_check = config["also_check"]

        # query window: 30 minutes before alert to 10 minutes after
        start = alert_time - timedelta(minutes=30)
        end = alert_time + timedelta(minutes=10)

        start_str = start.isoformat() + "Z"
        end_str = end.isoformat() + "Z"

        collected = []

        # query each metric, filtered to this specific host
        for metric in metrics:
            evidence = self._collect_metric(
                db=db,
                alert_id=alert_id,
                metric=metric,
                filter_label=filter_label,
                instance=instance,
                start=start_str,
                end=end_str,
            )
            if evidence:
                collected.append(evidence)

        # collect extra sources
        for source in also_check:
            evidence = self._collect_extra(
                db=db,
                alert_id=alert_id,
                source=source,
                instance=instance,
                alert_time=alert_time,
            )
            if evidence:
                collected.append(evidence)

        logger.info(f"Collected {len(collected)} evidence items "
                    f"for alert {alert_id}")

        return collected

    def _collect_metric(
        self,
        db: Session,
        alert_id: int,
        metric: str,
        filter_label: str,
        instance: str,
        start: str,
        end: str,
    ):
        """
        Query one Prometheus metric filtered to a specific host.
        Saves result as an Evidence row.
        """

        try:
            safe_instance = safe_label_value(instance)
        except UnsafeLabelValueError as e:
            logger.warning(f"Refusing to query metric {metric} for "
                            f"alert {alert_id}: {e}")
            return None

        # inject the label filter directly into the metric query
        if "{" in metric:
            # metric already has labels e.g. node_cpu{mode!='idle'}
            # insert our filter alongside existing ones
            filtered_query = metric.replace(
                "{", f'{{{filter_label}="{safe_instance}",'
            )
        else:
            # metric has no labels yet
            filtered_query = f'{metric}{{{filter_label}="{safe_instance}"}}'

        try:
            result = self.prometheus.range_query(
                query=filtered_query,
                start=start,
                end=end,
                step="30s"
            )

            values = result.get("data", {}).get("result", [])

            if not values:
                return None

            # compute a simple summary: min, max, avg of the values
            all_values = []
            for series in values:
                for _, v in series.get("values", []):
                    try:
                        all_values.append(float(v))
                    except (ValueError, TypeError):
                        pass

            summary = {}
            if all_values:
                summary = {
                    "min": round(min(all_values), 4),
                    "max": round(max(all_values), 4),
                    "avg": round(sum(all_values) / len(all_values), 4),
                    "samples": len(all_values),
                }

            evidence = Evidence(
                alert_id=alert_id,
                source="prometheus",
                metric_name=metric,
                raw_data=result,
                summary=summary,
                collected_at=datetime.utcnow(),
            )

            db.add(evidence)
            db.flush()

            return evidence

        except Exception as e:
            logger.error(f"Failed to collect metric {metric} "
                         f"for {instance}: {e}")
            return None

    def _collect_extra(
        self,
        db: Session,
        alert_id: int,
        source: str,
        instance: str,
        alert_time: datetime,
    ):
        """
        Collect non-Prometheus evidence: deployments, K8s events, OOMKills.
        Each source type has its own collection logic.
        """

        try:
            if source == "recent_deployments":
                return self._collect_deployments(
                    db, alert_id, alert_time
                )
            elif source == "pod_restarts":
                return self._collect_pod_restarts(
                    db, alert_id, instance, alert_time
                )
            elif source == "k8s_node_events":
                return self._collect_k8s_events(
                    db, alert_id, instance, alert_time
                )
            elif source == "oomkill_events":
                return self._collect_oomkills(
                    db, alert_id, instance, alert_time
                )
        except Exception as e:
            logger.error(f"Failed to collect {source} for "
                         f"alert {alert_id}: {e}")
            return None

    def _collect_deployments(
        self, db: Session, alert_id: int, alert_time: datetime
    ):
        """
        Check if any deployment happened within 15 minutes before the alert.
        Queries Prometheus for deployment-related metrics if available,
        otherwise stores a placeholder for manual enrichment.
        """

        start = alert_time - timedelta(minutes=15)
        end = alert_time

        # query kube_deployment_status_observed_generation
        # a change here = a deployment happened
        query = "changes(kube_deployment_status_observed_generation[15m])"

        try:
            result = self.prometheus.instant_query(query)
            deployments = result.get("data", {}).get("result", [])

            changed = [
                {
                    "deployment": d.get("metric", {}).get("deployment"),
                    "namespace": d.get("metric", {}).get("namespace"),
                    "changes": d.get("value", [None, "0"])[1],
                }
                for d in deployments
                if float(d.get("value", [None, "0"])[1]) > 0
            ]

            evidence = Evidence(
                alert_id=alert_id,
                source="deployments",
                metric_name="recent_deployments",
                raw_data={"deployments": changed},
                summary={
                    "deployment_count": len(changed),
                    "window_minutes": 15,
                    "has_recent_deployment": len(changed) > 0,
                },
                collected_at=datetime.utcnow(),
            )

            db.add(evidence)
            db.flush()
            return evidence

        except Exception as e:
            logger.error(f"Deployment check failed: {e}")
            return None

    def _collect_pod_restarts(
        self,
        db: Session,
        alert_id: int,
        instance: str,
        alert_time: datetime,
    ):
        """
        Check for pod restarts on the affected node around alert time.
        """

        # extract node name from instance (server-A:9100 → server-A)
        node = instance.split(":")[0]

        try:
            node = safe_label_value(node)
        except UnsafeLabelValueError as e:
            logger.warning(f"Refusing pod-restart check for alert "
                            f"{alert_id}: {e}")
            return None

        query = (
            f'increase(kube_pod_container_status_restarts_total'
            f'{{node="{node}"}}[30m])'
        )

        try:
            result = self.prometheus.instant_query(query)
            pods = result.get("data", {}).get("result", [])

            restarting = [
                {
                    "pod": p.get("metric", {}).get("pod"),
                    "container": p.get("metric", {}).get("container"),
                    "restarts": p.get("value", [None, "0"])[1],
                }
                for p in pods
                if float(p.get("value", [None, "0"])[1]) > 0
            ]

            evidence = Evidence(
                alert_id=alert_id,
                source="k8s",
                metric_name="pod_restarts",
                raw_data={"pods": restarting},
                summary={
                    "restarting_pod_count": len(restarting),
                    "node": node,
                },
                collected_at=datetime.utcnow(),
            )

            db.add(evidence)
            db.flush()
            return evidence

        except Exception as e:
            logger.error(f"Pod restart check failed: {e}")
            return None

    def _collect_k8s_events(
        self,
        db: Session,
        alert_id: int,
        instance: str,
        alert_time: datetime,
    ):
        """
        Check node-level K8s events (NotReady, MemoryPressure, DiskPressure).
        """

        node = instance.split(":")[0]

        try:
            node = safe_label_value(node)
        except UnsafeLabelValueError as e:
            logger.warning(f"Refusing k8s-events check for alert "
                            f"{alert_id}: {e}")
            return None

        query = f'kube_node_status_condition{{node="{node}",status="true"}}'

        try:
            result = self.prometheus.instant_query(query)
            conditions = result.get("data", {}).get("result", [])

            active_conditions = [
                {
                    "condition": c.get("metric", {}).get("condition"),
                    "value": c.get("value", [None, "0"])[1],
                }
                for c in conditions
                if float(c.get("value", [None, "0"])[1]) == 1
            ]

            evidence = Evidence(
                alert_id=alert_id,
                source="k8s",
                metric_name="node_conditions",
                raw_data={"conditions": active_conditions},
                summary={
                    "active_condition_count": len(active_conditions),
                    "node": node,
                    "conditions": [
                        c["condition"] for c in active_conditions
                    ],
                },
                collected_at=datetime.utcnow(),
            )

            db.add(evidence)
            db.flush()
            return evidence

        except Exception as e:
            logger.error(f"K8s events check failed: {e}")
            return None

    def _collect_oomkills(
        self,
        db: Session,
        alert_id: int,
        instance: str,
        alert_time: datetime,
    ):
        """
        Check for OOMKill events on the affected node.
        """

        node = instance.split(":")[0]

        try:
            node = safe_label_value(node)
        except UnsafeLabelValueError as e:
            logger.warning(f"Refusing OOMKill check for alert "
                            f"{alert_id}: {e}")
            return None

        query = (
            f'kube_pod_container_status_last_terminated_reason'
            f'{{node="{node}",reason="OOMKilled"}}'
        )

        try:
            result = self.prometheus.instant_query(query)
            oomkilled = result.get("data", {}).get("result", [])

            evidence = Evidence(
                alert_id=alert_id,
                source="k8s",
                metric_name="oomkill_events",
                raw_data={"oomkilled_pods": [
                    {
                        "pod": o.get("metric", {}).get("pod"),
                        "container": o.get("metric", {}).get("container"),
                    }
                    for o in oomkilled
                ]},
                summary={
                    "oomkill_count": len(oomkilled),
                    "node": node,
                },
                collected_at=datetime.utcnow(),
            )

            db.add(evidence)
            db.flush()
            return evidence

        except Exception as e:
            logger.error(f"OOMKill check failed: {e}")
            return None