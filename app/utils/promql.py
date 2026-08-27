# app/utils/promql.py

import re

# Prometheus label values from alert payloads are attacker/operator controlled
# (they come straight from Alertmanager's labels dict). Every place that builds
# a PromQL query by f-string interpolation needs to run values through this
# first, or a label like   server-A"} or absent(up) or vector(1) {"x="   would
# break out of the query.

_SAFE_LABEL_VALUE = re.compile(r'^[a-zA-Z0-9_.:\-/]+$')


class UnsafeLabelValueError(ValueError):
    pass


def safe_label_value(value: str) -> str:
    """
    Validate a value that's about to be interpolated into a PromQL label
    matcher. Raises UnsafeLabelValueError if it contains anything outside
    a conservative allowlist (no quotes, braces, backslashes, whitespace).

    This is deliberately an allowlist, not an escape function — PromQL has
    no single well-defined escaping rule the way SQL does, so refusing
    anything unexpected is safer than trying to sanitize it.
    """
    if not value or not _SAFE_LABEL_VALUE.match(value):
        raise UnsafeLabelValueError(f"unsafe label value: {value!r}")
    return value
