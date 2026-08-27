# app/services/report.py

from jinja2 import Template

CONFIDENCE_COLOR = {
    "high": "#c0392b",
    "medium": "#e67e22",
    "low": "#7f8c8d",
}

REPORT_TEMPLATE = Template("""
<html>
<body style="font-family: Arial, sans-serif; color: #222; max-width: 800px; margin: auto;">
  <h2 style="margin-bottom:0;">RCA Report — Window #{{ window.id }}</h2>
  <p style="color:#666; margin-top:4px;">
    {{ window.window_start.strftime('%Y-%m-%d %H:%M UTC') }}
    &mdash; {{ window.window_end.strftime('%H:%M UTC') }}
    &middot; {{ alerts|length }} alert(s) &middot; {{ findings|length }} root cause group(s)
  </p>
  <hr>

  {% for finding in findings %}
    {% set group = groups_by_id.get(finding.group_id) %}
    <div style="margin-bottom: 28px; border-left: 4px solid {{ confidence_color(finding.confidence) }}; padding-left: 14px;">
      <h3 style="margin-bottom:2px;">
        Group {{ loop.index }}: {{ finding.root_cause|replace('_',' ')|title }}
        <span style="font-size: 12px; font-weight: normal; color: {{ confidence_color(finding.confidence) }};">
          [{{ finding.confidence|upper }} CONFIDENCE]
        </span>
      </h3>

      <p><strong>Affected hosts:</strong> {{ finding.affected_hosts|unique|join(', ') }}</p>
      <p><strong>Alert types:</strong> {{ finding.alert_types|join(', ') }}</p>
      <p>{{ finding.root_cause_detail }}</p>

      {% if finding.timeline %}
      <p style="margin-bottom:4px;"><strong>Timeline</strong></p>
      <ul style="margin-top:0;">
        {% for event in finding.timeline %}
          <li><code>{{ event.time }}</code> — {{ event.event }}{% if event.host %} ({{ event.host }}){% endif %}</li>
        {% endfor %}
      </ul>
      {% endif %}

      {% if finding.suggested_actions %}
      <p style="margin-bottom:4px;"><strong>Suggested actions</strong></p>
      <ul style="margin-top:0;">
        {% for action in finding.suggested_actions %}
          <li>{{ action }}</li>
        {% endfor %}
      </ul>
      {% endif %}
    </div>
  {% endfor %}

  <hr>
  <p style="font-size:12px; color:#999;">Generated automatically by RCA-Tool.</p>
</body>
</html>
""")


def build_window_report(window, groups: list, findings: list, alerts: list) -> tuple[str, str]:
    """
    Render an HTML RCA report for a completed window.

    Returns (html_body, email_subject).
    """

    groups_by_id = {g.group_id: g for g in groups}

    root_causes = sorted({f.root_cause.replace("_", " ") for f in findings})
    subject = f"[RCA] Window #{window.id} — {len(alerts)} alert(s) — {', '.join(root_causes) or 'undetermined'}"

    html = REPORT_TEMPLATE.render(
        window=window,
        groups_by_id=groups_by_id,
        findings=findings,
        alerts=alerts,
        confidence_color=lambda c: CONFIDENCE_COLOR.get(c, "#7f8c8d"),
    )

    return html, subject
