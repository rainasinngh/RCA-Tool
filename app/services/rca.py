# app/services/rca.py

import logging
from datetime import datetime
from sqlalchemy.orm import Session

from ..models import Alert, Evidence, AlertGroup, RCAFinding

logger = logging.getLogger(__name__)


class RCAEngine:

    def analyze_group(
        self, db: Session, group: AlertGroup
    ) -> RCAFinding:
        """
        Main entry point.
        Analyze one group and produce a root cause finding.
        """

        logger.info(f"Analyzing group {group.group_id} "
                    f"with {len(group.alert_ids)} alerts")

        # load all alerts and their evidence for this group
        alerts = db.query(Alert).filter(
            Alert.id.in_(group.alert_ids)
        ).all()

        all_evidence = db.query(Evidence).filter(
            Evidence.alert_id.in_(group.alert_ids)
        ).all()

        # build evidence index: alert_id → list of evidence
        evidence_index = {}
        for ev in all_evidence:
            if ev.alert_id not in evidence_index:
                evidence_index[ev.alert_id] = []
            evidence_index[ev.alert_id].append(ev)

        # run root cause determination
        root_cause = self._determine_root_cause(
            group=group,
            alerts=alerts,
            evidence_index=evidence_index,
        )

        # build timeline
        timeline = self._build_timeline(
            alerts=alerts,
            evidence_index=evidence_index,
            root_cause=root_cause,
        )

        # build action items
        actions = self._suggest_actions(root_cause)

        # persist finding
        finding = RCAFinding(
            group_id=group.group_id,
            window_id=group.window_id,
            root_cause=root_cause["cause"],
            root_cause_detail=root_cause["detail"],
            confidence=root_cause["confidence"],
            affected_hosts=[a.instance for a in alerts],
            alert_types=list({a.alert_name for a in alerts}),
            timeline=timeline,
            suggested_actions=actions,
            created_at=datetime.utcnow(),
        )

        db.add(finding)
        db.commit()

        logger.info(f"RCA complete for group {group.group_id}: "
                    f"{root_cause['cause']}")

        return finding

    def _determine_root_cause(
        self,
        group: AlertGroup,
        alerts: list,
        evidence_index: dict,
    ) -> dict:
        """
        Apply root cause rules in order of confidence.

        Rule 1 — deployment cause
          Evidence shows a recent deployment AND
          suspected_cause from correlation = recent_deployment

        Rule 2 — OOMKill cause
          Evidence shows oomkill_count > 0

        Rule 3 — disk full cause
          Evidence shows filesystem < 10% free

        Rule 4 — node pressure cause
          K8s node conditions show MemoryPressure or DiskPressure

        Rule 5 — cascade cause
          HostDown followed by CPU/load spikes on other hosts
          = downstream cascade from a failed node

        Rule 6 — unknown
          No clear signal — report what was observed
        """

        # flatten all evidence summaries for easier inspection
        all_summaries = {}
        for ev_list in evidence_index.values():
            for ev in ev_list:
                if ev.metric_name not in all_summaries:
                    all_summaries[ev.metric_name] = []
                all_summaries[ev.metric_name].append(ev.summary)

        # Rule 1 — deployment
        if group.suspected_cause == "recent_deployment":
            deployment_info = all_summaries.get(
                "recent_deployments", [{}]
            )[0]
            return {
                "cause": "recent_deployment",
                "detail": (
                    f"A deployment was detected approximately "
                    f"{deployment_info.get('window_minutes', 15)} minutes "
                    f"before the alerts fired. "
                    f"{deployment_info.get('deployment_count', 'Unknown')} "
                    f"deployment(s) changed in this window."
                ),
                "confidence": "high",
            }

        # Rule 2 — OOMKill
        oomkill_summaries = all_summaries.get("oomkill_events", [])
        total_oomkills = sum(
            s.get("oomkill_count", 0) for s in oomkill_summaries
        )
        if total_oomkills > 0:
            return {
                "cause": "oom_kill",
                "detail": (
                    f"{total_oomkills} container(s) were OOMKilled. "
                    f"Memory limits were exceeded, causing the kernel "
                    f"to terminate processes."
                ),
                "confidence": "high",
            }

        # Rule 3 — disk full
        disk_summaries = all_summaries.get(
            "node_filesystem_avail_bytes", []
        )
        for summary in disk_summaries:
            if summary.get("min", 1) < 0.1:   # less than 10% free
                return {
                    "cause": "disk_full",
                    "detail": (
                        "Disk utilization reached critical levels. "
                        "Available filesystem space dropped below 10%. "
                        "This can cause process failures and host instability."
                    ),
                    "confidence": "high",
                }

        # Rule 4 — node pressure conditions
        condition_summaries = all_summaries.get("node_conditions", [])
        all_conditions = []
        for s in condition_summaries:
            all_conditions.extend(s.get("conditions", []))

        if "MemoryPressure" in all_conditions:
            return {
                "cause": "memory_pressure",
                "detail": (
                    "Kubernetes reported MemoryPressure on one or more nodes. "
                    "The node is running low on memory and may begin "
                    "evicting pods."
                ),
                "confidence": "high",
            }

        if "DiskPressure" in all_conditions:
            return {
                "cause": "disk_pressure",
                "detail": (
                    "Kubernetes reported DiskPressure on one or more nodes. "
                    "Available disk space is critically low."
                ),
                "confidence": "high",
            }

        # Rule 5 — cascade from host down
        alert_types = {a.alert_name for a in alerts}
        if "HostDown" in alert_types and len(alert_types) > 1:
            return {
                "cause": "cascade_from_host_failure",
                "detail": (
                    "One or more hosts went down, causing load redistribution "
                    "to remaining nodes. This triggered secondary CPU and "
                    "load alerts on the surviving nodes."
                ),
                "confidence": "medium",
            }

        # Rule 6 — high CPU with no clear cause
        cpu_summaries = all_summaries.get(
            "rate(node_cpu_seconds_total{mode!='idle'}[5m])", []
        )
        for summary in cpu_summaries:
            if summary.get("max", 0) > 0.9:
                return {
                    "cause": "cpu_saturation_unknown_trigger",
                    "detail": (
                        f"CPU utilization peaked at "
                        f"{round(summary['max']*100, 1)}%. "
                        f"No deployment or infrastructure event was "
                        f"identified as the trigger. Manual investigation "
                        f"of running processes at alert time is recommended."
                    ),
                    "confidence": "low",
                }

        # fallback
        return {
            "cause": "undetermined",
            "detail": (
                "Insufficient evidence to determine root cause automatically. "
                "Evidence has been collected and is available for "
                "manual review."
            ),
            "confidence": "low",
        }

    def _build_timeline(
        self,
        alerts: list,
        evidence_index: dict,
        root_cause: dict,
    ) -> list:
        """
        Build a chronological list of events around the incident.
        """

        events = []

        # add each alert as a timeline event
        for alert in alerts:
            if alert.starts_at:
                events.append({
                    "time": alert.starts_at.isoformat(),
                    "event": f"Alert fired: {alert.alert_name}",
                    "host": alert.instance,
                    "type": "alert",
                })

        # add deployment events if found
        for alert_id, ev_list in evidence_index.items():
            for ev in ev_list:
                if ev.metric_name == "recent_deployments":
                    dep_count = ev.summary.get(
                        "deployment_count", 0
                    )
                    if dep_count > 0:
                        events.append({
                            "time": "~15min before first alert",
                            "event": f"{dep_count} deployment(s) detected",
                            "host": "cluster",
                            "type": "deployment",
                        })

        # add OOMKill events
        for alert_id, ev_list in evidence_index.items():
            for ev in ev_list:
                if ev.metric_name == "oomkill_events":
                    count = ev.summary.get("oomkill_count", 0)
                    if count > 0:
                        events.append({
                            "time": "at alert time",
                            "event": f"{count} OOMKill(s) on "
                                     f"{ev.summary.get('node')}",
                            "host": ev.summary.get("node"),
                            "type": "oomkill",
                        })

        # sort by time where possible
        events.sort(key=lambda e: e["time"])

        return events

    def _suggest_actions(self, root_cause: dict) -> list:
        """
        Return a list of suggested action items based on root cause.
        """

        actions_map = {
            "recent_deployment": [
                "Rollback the deployment immediately if symptoms persist",
                "Review deployment diff for resource limit changes",
                "Add deployment health gates (canary or blue/green)",
                "Check resource requests/limits in the deployment manifest",
            ],
            "oom_kill": [
                "Increase memory limits for the affected containers",
                "Profile memory usage of the application",
                "Add memory usage alerting at 80% threshold",
                "Check for memory leaks in application code",
            ],
            "disk_full": [
                "Clear log files and temporary data immediately",
                "Add disk usage alerting at 80% threshold",
                "Review log rotation policies",
                "Consider increasing persistent volume size",
            ],
            "memory_pressure": [
                "Identify and evict low-priority pods",
                "Review resource requests for all pods on affected nodes",
                "Consider adding nodes to the cluster",
                "Enable pod disruption budgets",
            ],
            "cascade_from_host_failure": [
                "Investigate root cause of initial host failure",
                "Review cluster autoscaler configuration",
                "Ensure pod anti-affinity rules spread load across nodes",
                "Add capacity buffer to absorb single-node failure",
            ],
            "undetermined": [
                "Review application logs at the time of the incident",
                "Check for external dependency failures",
                "Add more granular metrics to improve future RCA",
            ],
        }

        return actions_map.get(
            root_cause["cause"],
            actions_map["undetermined"]
        )