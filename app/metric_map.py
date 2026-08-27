# app/metric_map.py

ALERT_METRIC_MAP = {

    "HostHighCPU": {
        # which Prometheus metrics to query
        "metrics": [
            "rate(node_cpu_seconds_total{mode!='idle'}[5m])",
            "node_load1",
            "node_load5",
            "node_load15",
        ],
        # which label from the alert to use as filter
        # instance = "server-A:9100"
        "filter_label": "instance",

        # extra sources to check beyond Prometheus
        "also_check": ["recent_deployments", "pod_restarts"],

        # human readable — used in the report
        "description": "CPU utilization spike",
    },

    "HostDown": {
        "metrics": [
            "up",
            "node_filesystem_avail_bytes",
            "node_network_receive_errs_total",
            "node_network_transmit_errs_total",
        ],
        "filter_label": "instance",
        "also_check": ["k8s_node_events", "recent_deployments"],
        "description": "Host unreachable or down",
    },

    "HostHighMemory": {
        "metrics": [
            "node_memory_MemAvailable_bytes",
            "node_memory_MemTotal_bytes",
            "node_memory_SwapUsed_bytes",
            "container_memory_usage_bytes",
        ],
        "filter_label": "instance",
        "also_check": ["oomkill_events", "pod_restarts"],
        "description": "Memory exhaustion or OOM pressure",
    },

    "HostHighLoad": {
        "metrics": [
            "node_load1",
            "node_load5",
            "node_load15",
            "node_disk_io_time_seconds_total",
            "rate(node_disk_read_bytes_total[5m])",
            "rate(node_disk_written_bytes_total[5m])",
        ],
        "filter_label": "instance",
        "also_check": ["recent_deployments"],
        "description": "System load average too high",
    },

    "HostDiskFull": {
        "metrics": [
            "node_filesystem_avail_bytes",
            "node_filesystem_size_bytes",
            "rate(node_filesystem_files_free[5m])",
        ],
        "filter_label": "instance",
        "also_check": [],
        "description": "Disk space critically low",
    },

    "ContainerRestarting": {
        "metrics": [
            "kube_pod_container_status_restarts_total",
            "container_memory_usage_bytes",
            "rate(container_cpu_usage_seconds_total[5m])",
        ],
        # for container alerts the label is 'pod' not 'instance'
        "filter_label": "pod",
        "also_check": ["k8s_node_events", "recent_deployments"],
        "description": "Container crash loop or repeated restarts",
    },

    "PodNotReady": {
        "metrics": [
            "kube_pod_status_ready",
            "kube_pod_container_status_waiting_reason",
            "kube_pod_status_phase",
        ],
        "filter_label": "pod",
        "also_check": ["k8s_node_events", "recent_deployments"],
        "description": "Pod stuck in not-ready state",
    },

    # fallback — unknown alert types still get basic system metrics
    "DEFAULT": {
        "metrics": [
            "up",
            "node_load1",
            "node_memory_MemAvailable_bytes",
        ],
        "filter_label": "instance",
        "also_check": [],
        "description": "Unknown alert type — basic metrics collected",
    },
}


def get_metric_config(alert_name: str) -> dict:
    """
    Look up the metric config for an alert type.
    Falls back to DEFAULT if alert type is not in the map.
    """
    return ALERT_METRIC_MAP.get(alert_name, ALERT_METRIC_MAP["DEFAULT"])