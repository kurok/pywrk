#!/usr/bin/env python3
"""Generate an interactive HTML report from pywrkr JSON results.

Usage: python3 generate_report.py results.json report.html
"""

import json
import sys
from pathlib import Path


def generate_html(data: dict) -> str:
    """Build a self-contained HTML report with Chart.js visualizations."""

    duration = data.get("duration_sec", 0)
    total_req = data.get("total_requests", 0)
    total_err = data.get("total_errors", 0)
    rps = data.get("requests_per_sec", 0)
    transfer = data.get("transfer_per_sec_bytes", 0)
    total_bytes = data.get("total_bytes", 0)
    connections = data.get("connections", 0)
    status_codes = data.get("status_codes", {})
    error_types = data.get("error_types", {})
    latency = data.get("latency", {})
    percentiles = data.get("percentiles", {})
    tags = data.get("tags", {})
    target_rps = data.get("target_rps")

    error_rate = (total_err / total_req * 100) if total_req > 0 else 0

    def fmt_bytes(b):
        if b >= 1_073_741_824:
            return f"{b / 1_073_741_824:.2f} GB"
        if b >= 1_048_576:
            return f"{b / 1_048_576:.2f} MB"
        if b >= 1024:
            return f"{b / 1024:.2f} KB"
        return f"{b} B"

    def fmt_ms(sec):
        ms = sec * 1000
        if ms >= 1000:
            return f"{ms / 1000:.2f}s"
        return f"{ms:.2f}ms"

    # Percentile chart data
    pct_labels = []
    pct_values = []
    for key in ["p50", "p75", "p90", "p95", "p99", "p99.9", "p99.99"]:
        if key in percentiles:
            pct_labels.append(key)
            pct_values.append(round(percentiles[key] * 1000, 2))

    # Status code chart data
    sc_labels = list(status_codes.keys())
    sc_values = list(status_codes.values())
    sc_colors = []
    for code in sc_labels:
        c = int(code)
        if 200 <= c < 300:
            sc_colors.append("'#22c55e'")
        elif 300 <= c < 400:
            sc_colors.append("'#3b82f6'")
        elif 400 <= c < 500:
            sc_colors.append("'#f59e0b'")
        else:
            sc_colors.append("'#ef4444'")

    # Error table rows
    error_rows = ""
    if error_types:
        for err_type, count in sorted(error_types.items(), key=lambda x: -x[1]):
            pct = count / total_req * 100 if total_req > 0 else 0
            error_rows += f"<tr><td>{err_type}</td><td>{count:,}</td><td>{pct:.2f}%</td></tr>\n"

    # Tags section
    tags_html = ""
    if tags:
        tags_html = (
            "<div class='tags'>"
            + " ".join(f"<span class='tag'>{k}={v}</span>" for k, v in tags.items())
            + "</div>"
        )

    pct_rows = "".join(
        f"<tr><td>{k}</td><td>{fmt_ms(v)}</td></tr>"
        for k, v in sorted(
            percentiles.items(),
            key=lambda x: float(x[0].replace("p", "")),
        )
    )
    error_section = (
        "<h2 class='section-title'>Errors</h2>"
        "<table><thead><tr><th>Error Type</th>"
        "<th>Count</th><th>% of Total</th></tr></thead>"
        "<tbody>" + error_rows + "</tbody></table>"
        if error_rows
        else ""
    )

    err_cls = "kpi-good" if error_rate < 1 else "kpi-warn" if error_rate < 5 else "kpi-bad"
    p95_val = percentiles.get("p95", 0)
    p95_cls = "kpi-good" if p95_val < 0.5 else "kpi-warn" if p95_val < 1 else "kpi-bad"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>pywrkr Load Test Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0f172a; color: #e2e8f0; padding: 24px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 8px; color: #f8fafc; }}
  .subtitle {{ color: #94a3b8; margin-bottom: 24px; font-size: 0.9rem; }}
  .tags {{ margin-bottom: 16px; }}
  .tag {{ background: #1e293b; border: 1px solid #334155; padding: 2px 8px;
          border-radius: 4px; font-size: 0.8rem; color: #94a3b8; margin-right: 6px; }}

  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
               gap: 16px; margin-bottom: 32px; }}
  .kpi {{ background: #1e293b; border-radius: 12px; padding: 20px; text-align: center; }}
  .kpi-value {{ font-size: 1.6rem; font-weight: 700; color: #f8fafc; }}
  .kpi-label {{ font-size: 0.8rem; color: #94a3b8; margin-top: 4px; text-transform: uppercase;
                letter-spacing: 0.05em; }}
  .kpi-good .kpi-value {{ color: #22c55e; }}
  .kpi-warn .kpi-value {{ color: #f59e0b; }}
  .kpi-bad .kpi-value {{ color: #ef4444; }}

  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 32px; }}
  .chart-card {{ background: #1e293b; border-radius: 12px; padding: 20px; }}
  .chart-card h3 {{ font-size: 1rem; margin-bottom: 16px; color: #f8fafc; }}
  .chart-card canvas {{ max-height: 300px; }}

  table {{ width: 100%; border-collapse: collapse; background: #1e293b; border-radius: 12px;
           overflow: hidden; margin-bottom: 24px; }}
  th {{ background: #334155; padding: 12px 16px; text-align: left; font-size: 0.8rem;
       text-transform: uppercase; letter-spacing: 0.05em; color: #94a3b8; }}
  td {{ padding: 10px 16px; border-top: 1px solid #334155; }}
  tr:hover {{ background: #1a2744; }}

  .section-title {{ font-size: 1.2rem; margin: 24px 0 12px; color: #f8fafc; }}
  .footer {{ text-align: center; margin-top: 32px; color: #475569; font-size: 0.8rem; }}

  @media (max-width: 768px) {{
    .charts {{ grid-template-columns: 1fr; }}
    .kpi-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>pywrkr Load Test Report</h1>
  <div class="subtitle">
    Duration: {duration:.1f}s &middot; Connections: {connections} &middot;
    {f"Target RPS: {target_rps}" if target_rps else "Unlimited RPS"}
  </div>
  {tags_html}

  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-value">{total_req:,}</div>
      <div class="kpi-label">Total Requests</div>
    </div>
    <div class="kpi {"kpi-good" if rps > 0 else ""}">
      <div class="kpi-value">{rps:,.1f}</div>
      <div class="kpi-label">Requests/sec</div>
    </div>
    <div class="kpi {err_cls}">
      <div class="kpi-value">{error_rate:.2f}%</div>
      <div class="kpi-label">Error Rate</div>
    </div>
    <div class="kpi">
      <div class="kpi-value">{fmt_ms(latency.get("mean", 0))}</div>
      <div class="kpi-label">Mean Latency</div>
    </div>
    <div class="kpi">
      <div class="kpi-value">{fmt_ms(latency.get("median", 0))}</div>
      <div class="kpi-label">p50 Latency</div>
    </div>
    <div class="kpi {p95_cls}">
      <div class="kpi-value">{fmt_ms(percentiles.get("p95", 0))}</div>
      <div class="kpi-label">p95 Latency</div>
    </div>
    <div class="kpi">
      <div class="kpi-value">{fmt_ms(percentiles.get("p99", 0))}</div>
      <div class="kpi-label">p99 Latency</div>
    </div>
    <div class="kpi">
      <div class="kpi-value">{fmt_bytes(transfer)}/s</div>
      <div class="kpi-label">Throughput</div>
    </div>
  </div>

  <div class="charts">
    <div class="chart-card">
      <h3>Latency Percentiles</h3>
      <canvas id="pctChart"></canvas>
    </div>
    <div class="chart-card">
      <h3>Status Code Distribution</h3>
      <canvas id="scChart"></canvas>
    </div>
  </div>

  <h2 class="section-title">Latency Breakdown</h2>
  <table>
    <thead>
      <tr><th>Metric</th><th>Value</th></tr>
    </thead>
    <tbody>
      <tr><td>Min</td><td>{fmt_ms(latency.get("min", 0))}</td></tr>
      <tr><td>Max</td><td>{fmt_ms(latency.get("max", 0))}</td></tr>
      <tr><td>Mean</td><td>{fmt_ms(latency.get("mean", 0))}</td></tr>
      <tr><td>Median</td><td>{fmt_ms(latency.get("median", 0))}</td></tr>
      <tr><td>Stdev</td><td>{fmt_ms(latency.get("stdev", 0))}</td></tr>
      {pct_rows}
    </tbody>
  </table>

  {error_section}

  <h2 class="section-title">Transfer</h2>
  <table>
    <thead><tr><th>Metric</th><th>Value</th></tr></thead>
    <tbody>
      <tr><td>Total Transferred</td><td>{fmt_bytes(total_bytes)}</td></tr>
      <tr><td>Transfer Rate</td><td>{fmt_bytes(transfer)}/s</td></tr>
      <tr><td>Total Errors</td><td>{total_err:,}</td></tr>
    </tbody>
  </table>

  <div class="footer">
    Generated by pywrkr &middot; {duration:.1f}s load test
  </div>
</div>

<script>
new Chart(document.getElementById('pctChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(pct_labels)},
    datasets: [{{
      label: 'Latency (ms)',
      data: {json.dumps(pct_values)},
      backgroundColor: 'rgba(99, 102, 241, 0.7)',
      borderColor: 'rgb(99, 102, 241)',
      borderWidth: 1,
      borderRadius: 4,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{ title: {{ display: true, text: 'ms', color: '#94a3b8' }},
            ticks: {{ color: '#94a3b8' }}, grid: {{ color: '#334155' }} }},
      x: {{ ticks: {{ color: '#94a3b8' }}, grid: {{ display: false }} }}
    }}
  }}
}});

new Chart(document.getElementById('scChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(sc_labels)},
    datasets: [{{
      data: {json.dumps(sc_values)},
      backgroundColor: [{", ".join(sc_colors)}],
      borderWidth: 0,
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{ position: 'bottom', labels: {{ color: '#94a3b8' }} }}
    }}
  }}
}});
</script>
</body>
</html>"""


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <results.json> <output.html>", file=sys.stderr)
        sys.exit(1)

    results_path = sys.argv[1]
    output_path = sys.argv[2]

    with open(results_path) as f:
        data = json.load(f)

    html = generate_html(data)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)
    print(f"HTML report written to: {output_path}")


if __name__ == "__main__":
    main()
