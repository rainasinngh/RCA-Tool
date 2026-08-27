# app/services/correlation.py

import logging
from datetime import timedelta
from itertools import groupby
from sqlalchemy.orm import Session

from ..models import Alert, Evidence, AlertGroup

logger = logging.getLogger(__name__)


class CorrelationEngine:

    # alerts within this many seconds of each other = possibly related
    TIME_WINDOW_SECONDS = 120

    def correlate_window(
        self, db: Session, window_id: int
    ) -> list:
        """
        Main entry point.
        Takes all alerts in a window, groups them by root cause.
        Returns list of AlertGroup objects.
        """

        alerts = db.query(Alert).filter(
            Alert.window_id == window_id
        ).order_by(Alert.starts_at).all()

        if not alerts:
            return []

        logger.info(f"Correlating {len(alerts)} alerts "
                    f"in window {window_id}")

        # build a rich dict per alert with its evidence
        enriched = []
        for alert in alerts:
            evidence = db.query(Evidence).filter(
                Evidence.alert_id == alert.id
            ).all()
            enriched.append(
                self._build_alert_profile(alert, evidence)
            )

        # run correlation passes in order of confidence
        groups = self._correlate(enriched)

        # persist groups to DB
        saved_groups = []
        for i, group in enumerate(groups):
            group_id = f"window-{window_id}-group-{i+1}"

            alert_group = AlertGroup(
                window_id=window_id,
                group_id=group_id,
                alert_ids=[p["alert_id"] for p in group["profiles"]],
                suspected_cause=group["suspected_cause"],
                confidence=group["confidence"],
                signal=group["signal"],
            )
            db.add(alert_group)

            # stamp each alert with its group
            for profile in group["profiles"]:
                db.query(Alert).filter(
                    Alert.id == profile["alert_id"]
                ).update({"group_id": group_id})

            saved_groups.append(alert_group)

        db.commit()
        logger.info(f"Created {len(saved_groups)} groups "
                    f"for window {window_id}")

        return saved_groups

    def _build_alert_profile(
        self, alert, evidence_list: list
    ) -> dict:
        """
        Build a summary dict per alert that the correlation
        logic can reason about.
        """

        profile = {
            "alert_id": alert.id,
            "alert_name": alert.alert_name,
            "instance": alert.instance,
            "starts_at": alert.starts_at,
            "node": alert.instance.split(":")[0]
                    if alert.instance else None,
            "evidence": {},
        }

        for ev in evidence_list:
            profile["evidence"][ev.metric_name] = ev.summary

        return profile

    def _correlate(self, profiles: list) -> list:
        """
        Run three correlation passes in order.
        Earlier passes have higher confidence.

        Pass 1 — deployment correlation
          If a recent deployment is found in the evidence of
          multiple alerts → same cause

        Pass 2 — time + node pool correlation
          Alerts that spiked within 120 seconds of each other
          AND are on the same node pool → same cause

        Pass 3 — alert type grouping
          Remaining ungrouped alerts of the same type →
          probably same cause

        Anything left ungrouped → individual groups
        """

        ungrouped = list(profiles)
        groups = []

        # Pass 1 — deployment signal
        deployment_group = self._group_by_deployment(ungrouped)
        if deployment_group:
            groups.append(deployment_group)
            grouped_ids = {
                p["alert_id"]
                for p in deployment_group["profiles"]
            }
            ungrouped = [
                p for p in ungrouped
                if p["alert_id"] not in grouped_ids
            ]

        # Pass 2 — time proximity + same node
        time_groups = self._group_by_time_and_node(ungrouped)
        for g in time_groups:
            if len(g["profiles"]) > 1:
                groups.append(g)
                grouped_ids = {
                    p["alert_id"] for p in g["profiles"]
                }
                ungrouped = [
                    p for p in ungrouped
                    if p["alert_id"] not in grouped_ids
                ]

        # Pass 3 — same alert type
        type_groups = self._group_by_alert_type(ungrouped)
        for g in type_groups:
            if len(g["profiles"]) > 1:
                groups.append(g)
                grouped_ids = {
                    p["alert_id"] for p in g["profiles"]
                }
                ungrouped = [
                    p for p in ungrouped
                    if p["alert_id"] not in grouped_ids
                ]

        # anything left = its own group
        for profile in ungrouped:
            groups.append({
                "profiles": [profile],
                "suspected_cause": "isolated_incident",
                "confidence": "low",
                "signal": "no_correlation_found",
            })

        return groups

    def _group_by_deployment(self, profiles: list) -> dict | None:
        """
        If multiple alerts have recent deployment evidence
        with deployment_count > 0, they share a deployment cause.
        """

        deployment_affected = [
            p for p in profiles
            if p["evidence"].get(
                "recent_deployments", {}
            ).get("has_recent_deployment", False)
        ]

        if len(deployment_affected) < 2:
            return None

        return {
            "profiles": deployment_affected,
            "suspected_cause": "recent_deployment",
            "confidence": "high",
            "signal": "deployment_preceded_alerts",
        }

    def _group_by_time_and_node(
        self, profiles: list
    ) -> list:
        """
        Group alerts that fired within TIME_WINDOW_SECONDS
        of each other. Within that set, sub-group by node name
        (node = first part of instance before colon).
        """

        if not profiles:
            return []

        # sort by time
        sorted_profiles = sorted(
            profiles,
            key=lambda p: p["starts_at"] or 0
        )

        clusters = []
        current_cluster = [sorted_profiles[0]]

        for profile in sorted_profiles[1:]:
            prev_time = current_cluster[-1]["starts_at"]
            curr_time = profile["starts_at"]

            if prev_time and curr_time:
                diff = abs(
                    (curr_time - prev_time).total_seconds()
                )
                if diff <= self.TIME_WINDOW_SECONDS:
                    current_cluster.append(profile)
                    continue

            clusters.append(current_cluster)
            current_cluster = [profile]

        clusters.append(current_cluster)

        # within each cluster sub-group by node pool prefix
        # e.g. node-pool-A-1 and node-pool-A-2 → same pool
        groups = []
        for cluster in clusters:
            node_groups: dict = {}
            for profile in cluster:
                node = profile.get("node") or "unknown"
                # use first segment as pool key
                pool = "-".join(node.split("-")[:3])
                if pool not in node_groups:
                    node_groups[pool] = []
                node_groups[pool].append(profile)

            for pool, pool_profiles in node_groups.items():
                groups.append({
                    "profiles": pool_profiles,
                    "suspected_cause": "infrastructure_event",
                    "confidence": "medium",
                    "signal": f"time_proximity_same_pool_{pool}",
                })

        return groups

    def _group_by_alert_type(
        self, profiles: list
    ) -> list:
        """
        Group remaining alerts by alert type.
        HostHighCPU x5 on different nodes → same systemic cause.
        """

        groups = []
        sorted_profiles = sorted(
            profiles, key=lambda p: p["alert_name"]
        )

        for alert_type, group_iter in groupby(
            sorted_profiles, key=lambda p: p["alert_name"]
        ):
            group_profiles = list(group_iter)
            groups.append({
                "profiles": group_profiles,
                "suspected_cause": f"systemic_{alert_type.lower()}",
                "confidence": "medium",
                "signal": f"same_alert_type_{alert_type}",
            })

        return groups