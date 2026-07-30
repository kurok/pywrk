# pywrkr

[![CI](https://github.com/kurok/pywrkr/actions/workflows/python-package.yml/badge.svg)](https://github.com/kurok/pywrkr/actions/workflows/python-package.yml)
[![CodeQL](https://github.com/kurok/pywrkr/actions/workflows/codeql.yml/badge.svg)](https://github.com/kurok/pywrkr/actions/workflows/codeql.yml)
[![Latest release](https://img.shields.io/github/v/release/kurok/pywrkr)](https://github.com/kurok/pywrkr/releases/latest)
[![Python versions](https://img.shields.io/pypi/pyversions/pywrkr)](https://pypi.org/project/pywrkr/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![codecov](https://codecov.io/gh/kurok/pywrkr/graph/badge.svg)](https://codecov.io/gh/kurok/pywrkr)

**Load-test any HTTP endpoint in one command — and get wrk/ab-grade numbers without the wrk/ab setup.** `pywrkr` is a pure-Python benchmarking CLI: point it at a URL and get latency percentiles (p50–p99.99), a throughput timeline, status/error breakdowns, and CI-ready SLO checks.

Five load modes (duration, fixed-count, virtual users, constant rate, traffic profiles), HAR-file import to turn a browser recording into a test, and OpenTelemetry / Prometheus export — no JVM, no YAML, no cluster.

## Demo

![Benchmarking an HTTP endpoint with pywrkr: a live requests/sec counter during the run, then a full report with latency percentiles, a status-code breakdown, and a throughput timeline](https://raw.githubusercontent.com/kurok/pywrkr/main/docs/assets/demo.gif)

<sub>Recorded with [asciinema](https://asciinema.org) + [agg](https://github.com/asciinema/agg) — regenerate with `docs/record-demo.sh`.</sub>

## Install

```bash
pip install pywrkr
```

## Minimal example

```bash
# 10 connections, 5-second benchmark
pywrkr https://example.com -c 10 -d 5
```

That's it. Add `--json results.json`, `-w report.html`, `--threshold "p95<300ms"`, or `-u 1000` for virtual users when you need more — see [Quick Start](#quick-start) below.

> **See also:** [awesome-http-benchmark](https://github.com/denji/awesome-http-benchmark) — a curated list of HTTP(S) load & benchmarking tools (wrk, ab, k6, vegeta, …) where pywrkr fits in.

## Features

- **HAR import** (`har-import`): convert browser-recorded HAR files into pywrkr scenarios or URL lists — dramatically cuts test authoring time
- **Scripted scenarios** (`--scenario`): multi-step flows with variable extraction and `${var}` correlation — log in, capture the token, and hit authenticated endpoints with it
- **Library API** (`pywrkr.run` / `pywrkr.arun`): pure Python, so load tests live inside pytest suites, notebooks, and CI scripts — typed `Result`, thresholds as verdicts instead of `exit()`
- **HTTP/2** (`--http2`): protocol-representative load against modern edges, via a pluggable client backend — the negotiated protocol is reported, never assumed
- **Per-user sessions:** each virtual user keeps its own cookie jar, so cookie-session logins work and N users look like N real clients rather than one anonymous loop
- **Data-driven testing** (`--data`): CSV/JSON feeders with `loop`/`sequential`/`random`/`unique` strategies plus built-in generators (`${uuid()}`, `${randint()}`, `${counter()}`, …) — 1000 users with 1000 distinct payloads, not 1000 copies of one request
- **Five benchmarking modes:**
  - **Duration mode** (`-d`): wrk-style, run for N seconds
  - **Request-count mode** (`-n`): ab-style, send exactly N requests
  - **User simulation mode** (`-u`): simulate virtual users with ramp-up and think time
  - **Rate limiting mode** (`--rate`): send requests at a controlled, constant rate (with optional ramp)
  - **Traffic profiles** (`--traffic-profile`): realistic traffic shaping — sine waves, spikes, step functions, business-hour curves, and CSV replay
  - **Autofind mode** (`--autofind`): automatically ramp load to find maximum sustainable capacity
- **Detailed latency statistics:** min/max/mean/median/stdev, percentiles (p50-p99.99), histogram, and ab-style "percentage served within" table
- **Throughput timeline:** requests/sec over time in ASCII bar chart
- **Multiple output formats:** terminal, CSV (`-e`), JSON (`--json`), HTML (`-w`)
- **HTTP features:** keep-alive toggle, Basic auth (`-A`), cookies (`-C`), custom headers (`-H`), POST body (`-b`/`-p`), content-length verification (`-l`)
- **Cache-busting** (`-R`): append a unique random query parameter to each request URL
- **Graceful shutdown:** handles SIGINT/SIGTERM cleanly
- **Live progress display** with requests/sec, error count, and active user count
- **SLO-aware thresholds** (`--threshold`): pass/fail criteria like `p95 < 300ms`, `error_rate < 1%` with non-zero exit code on breach — CI-ready
- **Regression detection** (`pywrkr compare`, `--baseline`): gate a PR on *relative* change — "fail if p95 got 10% worse than main" — with a markdown delta table for the PR comment
- **Native observability export:** OpenTelemetry (`--otel-endpoint`) and Prometheus remote write (`--prom-remote-write`), streamed live during the run with `--export-interval` — windowed percentiles, cumulative counters
- **Test metadata tags** (`--tag`): attach environment, build, region labels to metrics and JSON output

### HAR / Browser-Recording Import

Convert browser-recorded [HAR files](http://www.softwareishard.com/blog/har-12-spec/) (from Chrome DevTools, Firefox, Charles Proxy, Fiddler, etc.) into pywrkr scenarios or URL lists. Similar to k6's HAR converter and JMeter's HTTP(S) Test Script Recorder.

```bash
# Convert HAR to a pywrkr scenario (JSON):
pywrkr har-import recording.har -o scenario.json

# Then run the generated scenario (URLs come from the scenario):
pywrkr --scenario scenario.json -u 100 -d 60

# Or convert to a URL file for --url-file mode:
pywrkr har-import recording.har --format url-file -o urls.txt
pywrkr --url-file urls.txt -c 50 -d 30
```

**Recording a HAR file:**

1. Open Chrome DevTools (F12) → Network tab
2. Navigate through your application
3. Right-click the network log → "Save all as HAR with content"

**Filtering options:**

```bash
# Only include requests to specific domain(s):
pywrkr har-import recording.har --domain api.example.com -o scenario.json

# Include static assets (CSS, JS, images — excluded by default):
pywrkr har-import recording.har --include-static -o scenario.json

# Exclude analytics/tracking URLs:
pywrkr har-import recording.har --exclude '/analytics' --exclude '/tracking' -o scenario.json

# Only include specific URL patterns:
pywrkr har-import recording.har --include '/api/v2' -o scenario.json

# Preserve original request headers (default: only Content-Type):
pywrkr har-import recording.har --preserve-headers -o scenario.json

# Add status code assertions from recorded responses:
pywrkr har-import recording.har --assert-status -o scenario.json

# Adjust think time (inter-request delay derived from recording):
pywrkr har-import recording.har --think-time-multiplier 0.5 -o scenario.json   # 2x faster
pywrkr har-import recording.har --no-think-time -o scenario.json               # no delays
```

**HAR import options:**

| Flag | Description |
|------|-------------|
| `har_file` | Path to the HAR file (positional, required) |
| `-o` / `--output` | Output file path (default: print to stdout) |
| `--format` | Output format: `scenario` (default) or `url-file` |
| `--name` | Scenario name (default: derived from filename) |
| `--include-static` | Include static assets (CSS, JS, images, fonts) |
| `--domain` | Only include requests to this domain (repeatable) |
| `--exclude` | Exclude URLs matching regex pattern (repeatable) |
| `--include` | Only include URLs matching regex pattern (repeatable) |
| `--preserve-headers` | Keep original request headers |
| `--no-think-time` | Don't derive think times from recorded timing |
| `--think-time-multiplier` | Scale derived think times (default: 1.0) |
| `--assert-status` | Assert recorded 2xx/3xx status codes |

## Library usage

pywrkr is pure Python, so a load test can live *inside* a pytest suite, a notebook, or an
orchestration script — no subprocess, no JSON parsing:

```python
import pywrkr

result = pywrkr.run("https://api.example.com/health", connections=50, duration=30)

assert result.percentiles.p95 < 0.3
assert result.error_rate < 1.0
print(f"{result.requests_per_sec:,.0f} req/s over {result.duration:.1f}s")
```

Nothing is printed, no signal handlers are installed, and a breached threshold comes back as a
**verdict on the result** rather than an `exit()`:

```python
result = pywrkr.run(url, duration=30, thresholds=["p95 < 300ms", "error_rate < 1%"])
for verdict in result.thresholds:
    print(verdict.expression, "->", "pass" if verdict.passed else "FAIL", verdict.actual)
if not result.passed:
    raise SystemExit(result.exit_code)  # same code the CLI would use
```

**Async-native.** `arun()` never calls `asyncio.run`, so it is safe to await inside an existing
loop; `run()` raises a clear error if called from one.

```python
results = await asyncio.gather(
    pywrkr.arun(url, connections=5, duration=30),
    pywrkr.arun(url, connections=50, duration=30),
)
```

**Full control** via `Config`, which is the same object the CLI builds — anything the CLI can
express, the library can:

```python
config = pywrkr.Config(
    url="https://api.example.com",
    users=100,
    ramp_up=10,
    think_time=0.5,
    scenario=pywrkr.load_scenario("flow.yaml"),
)
result = await pywrkr.arun(config)
print(result.steps["checkout"]["p95"])
```

**Live progress** through `on_tick`, called about once a second (an exception from it is logged,
not fatal):

```python
pywrkr.run(url, duration=60, on_tick=lambda s: print(s.elapsed, s.requests_per_sec))
```

### API reference

| Name | What it is |
|------|-----------|
| `run(target, **opts) -> Result` | Blocking run. Raises `RuntimeError` inside a running loop |
| `arun(target, **opts) -> Result` | Async run, awaitable from an existing loop |
| `Config` | The run configuration (alias of `BenchmarkConfig`) — every CLI option is a field |
| `Result` | Typed results; see below |
| `Latency` / `Percentiles` / `ThresholdVerdict` / `LiveStats` | Result components |
| `load_scenario(path) -> Scenario` | Load a JSON/YAML scenario file |

`target` is a URL string plus keyword options, or a prepared `Config`. `thresholds` accepts
expression strings (`"p95 < 300ms"`) as well as parsed objects.

`Result` exposes `total_requests`, `total_errors`, `error_rate`, `requests_per_sec`, `duration`,
`total_bytes`, `latency`, `percentiles`, `status_codes`, `error_types`, `http_versions`,
`rps_timeline`, `steps`, `thresholds`, `passed`, `exit_code`, and the raw `stats`.

**`result.to_dict()` is exactly what `--json` writes** — same schema, same `schema_version` — so a
result can be fed straight to `pywrkr compare`, a dashboard, or a golden file. `to_json()`
serializes it identically.

`percentiles` is both attribute- and key-addressed, so tail percentiles that only exist for large
samples stay reachable: `result.percentiles.p95`, `result.percentiles["p99.9"]`.

### Stability

`pywrkr.__all__` is the supported surface and is what the versioning promise covers: breaking
changes to those names require a major release. Everything else is an implementation detail. A
few worker internals that leaked into the package namespace before this API existed
(`pywrkr.worker`, `pywrkr.make_url`, …) still import for one more minor release but emit a
`DeprecationWarning` pointing at `pywrkr.workers`.

The package ships a `py.typed` marker, so type checkers see the annotations.

Runnable examples: [`examples/library_usage.py`](examples/library_usage.py).

## Requirements

- Python 3.10+

```bash
pip install pywrkr
```

## Quick Start

```bash
# Basic 10-second benchmark with 10 connections
pywrkr http://localhost:8080/

# 30 seconds, 200 concurrent connections
pywrkr -c 200 -d 30 http://localhost:8080/api

# Send exactly 1000 requests with 50 connections (ab-style)
pywrkr -n 1000 -c 50 http://localhost:8080/

# Simulate 1500 users for 5 minutes with 30s ramp-up and 1s think time
pywrkr -u 1500 -d 300 --ramp-up 30 --think-time 1.0 http://localhost:8080/

# Cache-busting mode (bypass HTTP caches with random query param)
pywrkr -R -c 100 -d 10 http://localhost:8080/

# Constant rate: 500 requests/sec for 30 seconds
pywrkr --rate 500 -d 30 http://localhost:8080/

# Rate ramp: linearly increase from 100 to 1000 req/s over 60 seconds
pywrkr --rate 100 --rate-ramp 1000 -d 60 http://localhost:8080/

# Traffic profiles: sine wave oscillating up to 500 req/s
pywrkr --rate 500 -d 120 --traffic-profile sine http://localhost:8080/

# Traffic profiles: periodic spikes at 5x baseline
pywrkr --rate 200 -d 60 --traffic-profile "spike:interval=10,multiplier=5" http://localhost:8080/

# Traffic profiles: replay production traffic from CSV
pywrkr --rate 1000 -d 300 --traffic-profile "csv:traffic.csv" http://localhost:8080/

# Autofind: automatically find max sustainable load
pywrkr --autofind --max-error-rate 1 --max-p95 5.0 http://localhost:8080/

# SLO thresholds: exit code 2 if any threshold breached (CI-friendly)
pywrkr --threshold "p95 < 300ms" --threshold "error_rate < 1%" \
    -c 100 -d 30 http://localhost:8080/

# Export metrics to OpenTelemetry collector
pywrkr --otel-endpoint http://localhost:4318 \
    --tag environment=staging --tag build=v1.2.3 \
    -c 100 -d 30 http://localhost:8080/

# Push metrics to Prometheus Pushgateway
pywrkr --prom-remote-write http://pushgateway:9091 \
    --tag region=us-east-1 --tag service=api \
    -c 100 -d 30 http://localhost:8080/

# POST with auth, cookies, and JSON output
pywrkr -n 500 -c 20 -m POST -b '{"key":"val"}' \
    -H "Content-Type: application/json" \
    -A user:pass -C "session=abc123" \
    --json results.json http://localhost:8080/api
```

## Usage

```
usage: pywrkr [-h] [-c CONNECTIONS] [-d DURATION] [-n NUM_REQUESTS]
              [-t THREADS] [-m METHOD] [-H NAME:VALUE] [-b BODY]
              [-p POST_FILE] [-A user:pass] [-C COOKIE] [-k]
              [--no-keepalive] [-l] [-v VERBOSITY] [--timeout TIMEOUT]
              [--ssl-verify] [--ca-bundle FILE] [-R] [-e FILE] [-w]
              [--json FILE] [--html-report FILE] [--live]
              [--latency-breakdown] [--tag TAGS] [--otel-endpoint URL]
              [--prom-remote-write URL] [--threshold THRESHOLDS]
              [-u USERS] [--ramp-up RAMP_UP] [--think-time THINK_TIME]
              [--think-jitter THINK_JITTER] [--rate RATE]
              [--rate-ramp RATE_RAMP] [--traffic-profile PROFILE]
              [--scenario FILE] [--autofind]
              [--max-error-rate MAX_ERROR_RATE] [--max-p95 MAX_P95]
              [--step-duration STEP_DURATION] [--start-users START_USERS]
              [--max-users MAX_USERS] [--step-multiplier STEP_MULTIPLIER]
              [--url-file FILE] [--master] [--worker HOST:PORT]
              [--expect-workers N] [--bind ADDR] [--port PORT]
              [url]
```

### Options

| Flag | Long | Description |
|------|------|-------------|
| `url` | | Target URL to benchmark (required) |
| `-c` | `--connections` | Number of concurrent connections (default: 10) |
| `-d` | `--duration` | Test duration in seconds (default: 10) |
| `-n` | `--num-requests` | Total number of requests (ab-style, overrides `-d`) |
| `-t` | `--threads` | Number of worker groups (default: 4) |
| `-m` | `--method` | HTTP method: GET, POST, PUT, DELETE, etc. (default: GET) |
| `-H` | `--header` | Custom header, e.g. `-H "Content-Type: application/json"` (repeatable) |
| `-b` | `--body` | Request body string |
| `-p` | `--post-file` | File containing POST body data |
| `-A` | `--basic-auth` | Basic HTTP auth as `user:pass` |
| `-C` | `--cookie` | Cookie as `name=value` (repeatable) — always sent, in every mode |
| | `--no-session-cookies` | Ignore `Set-Cookie`. By default each virtual user keeps its own cookie jar |
| | `--http2` | Use the HTTP/2 backend (needs `pywrkr[http2]`). `-c` then bounds concurrent streams, not connections |
| | `--data` | Attach a CSV/JSON data set as `NAME=FILE` (repeatable), referenced as `${NAME.column}`. Requires `--scenario` |
| | `--data-strategy` | Row hand-out strategy as `NAME=STRATEGY` (repeatable): `loop`, `sequential`, `random`, `unique` |
| | `--baseline` | Compare against previous `--json` results (file or glob to average) and apply `--fail-on` |
| | `--save-baseline` | Write this run's results to a file for later `--baseline` comparison |
| | `--fail-on` | Regression rule on the baseline delta (repeatable), e.g. `"p95 > +10%"`. Exit code 3 when one fires |
| | `--strict-config` | Fail instead of warning when the baseline used a different load shape |
| | `--compare-format` | Baseline comparison format: `table` (default), `markdown`, `json` |
| `-k` | `--keepalive` | Enable keep-alive (default: on) |
| | `--no-keepalive` | Disable keep-alive |
| `-l` | `--verify-length` | Verify response Content-Length consistency |
| `-v` | `--verbosity` | 0=quiet, 2=warnings, 3=status codes, 4=full detail |
| | `--timeout` | Request timeout in seconds (default: 30) |
| `-e` | `--csv` | Write CSV percentile table to file |
| `-w` | `--html` | Print results as HTML table |
| | `--json` | Write JSON results to file |
| `-R` | `--random-param` | Append unique `_cb=<uuid>` query param per request (cache-buster) |
| | `--rate` | Target requests per second (constant rate mode) |
| | `--rate-ramp` | Linearly ramp rate from `--rate` to this value over the duration |
| | `--traffic-profile` | Traffic shaping profile: `sine`, `step`, `sawtooth`, `square`, `spike`, `business-hours`, or `csv:file.csv` |
| | `--html-report` | Generate interactive Gatling-style HTML report to file |
| | `--live` | Live TUI dashboard during benchmark (requires `pywrkr[tui]`) |
| | `--scenario` | Path to JSON/YAML scenario file for scripted multi-step requests (supports `extract` + `${var}` correlation) |
| | `--latency-breakdown` | Show detailed per-phase latency breakdown (DNS, TCP, TLS, TTFB, transfer) |
| | `--threshold` / `--th` | SLO threshold (repeatable), e.g. `--threshold "p95 < 300ms"`. Exit code 2 on breach |
| | `--tag` | Metadata tag as `key=value` (repeatable), e.g. `--tag environment=staging` |
| | `--otel-endpoint` | Export metrics to OpenTelemetry collector (OTLP/HTTP) |
| | `--prom-remote-write` | Push metrics to Prometheus Pushgateway endpoint |
| | `--export-interval` | Stream metric snapshots every N seconds instead of only at the end (needs an export endpoint) |
| | `--ssl-verify` / `PYWRKR_SSL_VERIFY` | Enable TLS certificate verification (default: disabled). Recommended when using `--basic-auth` or `--cookie` against `https://` targets |
| | `--ca-bundle PATH` / `PYWRKR_CA_BUNDLE` | Path to a custom CA certificate bundle (PEM format). Used when `--ssl-verify` is enabled and the target uses a private or corporate CA |

### User Simulation Options

| Flag | Long | Description |
|------|------|-------------|
| `-u` | `--users` | Number of virtual users (enables simulation mode) |
| | `--ramp-up` | Seconds to gradually start all users (default: 0) |
| | `--think-time` | Mean pause between requests per user in seconds (default: 1.0) |
| | `--think-jitter` | Think time jitter factor 0-1 (default: 0.5, i.e. +/-50%) |

## Output

### Terminal Output

```
======================================================================
  BENCHMARK RESULTS
======================================================================
  Mode:              300 virtual users, 120.0s
  Duration:          124.15s
  Virtual Users:     300
  Ramp-up:           10.00s
  Think Time:        1.00s (+/-50%)
  Avg Reqs/User:     50.8
  Keep-Alive:        yes
  Total Requests:    15,229
  Total Errors:      1
  Requests/sec:      122.66
  Transfer/sec:      119.34MB/s
  Total Transfer:    14.46GB

======================================================================
  LATENCY STATISTICS
======================================================================
    Min:          449.00ms
    Max:            4.85s
    Mean:           961.00ms
    Median:         870.00ms
    Stdev:          520.00ms

  Latency Percentiles:
    p50           870.00ms
    p75             1.10s
    p90             1.56s
    p95             2.98s
    p99             4.85s
```

### JSON Output

Use `--json results.json` to save structured results:

```json
{
  "duration_sec": 124.15,
  "connections": 300,
  "total_requests": 15229,
  "total_errors": 1,
  "requests_per_sec": 122.66,
  "transfer_per_sec_bytes": 125120000.0,
  "total_bytes": 15533200000,
  "latency": {
    "min": 0.449,
    "max": 4.85,
    "mean": 0.961,
    "median": 0.87,
    "stdev": 0.52
  },
  "percentiles": {
    "p50": 0.87,
    "p75": 1.1,
    "p90": 1.56,
    "p95": 2.98,
    "p99": 4.85
  }
}
```

## Benchmarking Modes

### Duration Mode (wrk-style)

Runs for a fixed duration with a pool of persistent connections:

```bash
pywrkr -c 100 -d 30 http://localhost:8080/
```

### Request-Count Mode (ab-style)

Sends exactly N requests, then stops:

```bash
pywrkr -n 10000 -c 50 http://localhost:8080/
```

### User Simulation Mode

Simulates realistic user behavior with configurable think time and gradual ramp-up:

```bash
pywrkr -u 500 -d 300 --ramp-up 30 --think-time 1.0 http://localhost:8080/
```

Each virtual user:
1. Sends a request
2. Waits for the response
3. Pauses for think time (with jitter)
4. Repeats until duration expires

The ramp-up period gradually introduces users to avoid a thundering herd at startup.

### Scripted Scenarios

A scenario file (JSON or YAML) describes a multi-step flow that every virtual user replays in a loop:

```bash
pywrkr --scenario examples/scenario-correlation.json -u 100 -d 60
```

Each step takes a `path`, plus optional `method`, `headers`, `body`, `think_time`, `name`,
`assert_status`, and `assert_body_contains`. The target host comes from the positional URL, or
from the scenario's own `base_url` when no URL is given.

#### Step assertions

`assert_status` alone lets a load test pass while the API returns well-formed garbage. Each step
can check what actually makes a response correct:

```yaml
steps:
  - name: get-user
    path: /users/42
    assert_status: 200
    assert_body_contains: "email"
    assert_body_regex: '"id":\s*42'
    assert_json:
      "$.id": 42          # must equal
      "$.email": "*"      # must exist, any value
    assert_header:
      X-Trace: "abc123"                    # exact match
      Content-Type: {regex: "^application/json"}   # or a regex
    assert_max_latency: 500ms
```

| Assertion | Checks |
|-----------|--------|
| `assert_status` | Exact status code |
| `assert_body_contains` | Substring is present in the body |
| `assert_body_regex` | Regex matches somewhere in the body |
| `assert_json` | JSONPath → expected value, or `"*"` for "must exist" |
| `assert_header` | Header equals a string, or matches `{regex: "..."}` |
| `assert_max_latency` | This request took no longer than `500ms` / `1.5s` / `250us` |

`assert_json` uses the same JSONPath subset as `extract`, so the two never disagree. Numbers
compare numerically (`42` matches `42.0`), but booleans stay distinct from numbers — `true` does
not satisfy an expected `1`.

Exact `assert_header` matches are exact: a server sending `application/json; charset=utf-8` will
*not* match `"application/json"`. Use the regex form for prefixes.

A failed assertion counts the request as **one** error however many rules broke, and each broken
rule gets its own key in the error distribution. Those keys name the *rule*, never the observed
value — otherwise a per-request latency or payload id would mint a fresh key every time and
overflow the breakdown. The observed value goes to the log at `-v 2`.

Bad regexes, unsupported JSONPaths, and nonsense durations are rejected when the scenario file
loads, naming the step.

#### Per-step reporting

Scenario runs report each step separately, because an aggregate p95 blends them: if `login` is
40ms and `checkout` is 2s, the headline number describes neither.

```
  PER-STEP BREAKDOWN
    Step          Count   Errors      Req/s         p50         p95         p99         Max
    get-user         35        0       17.3    509.00us    872.00us    894.00us    894.00us
    checkout         34       34       16.8     52.67ms     53.10ms     53.10ms     53.10ms
```

The same blocks appear under `step_stats` in `--json` (with `count`, `errors`, `requests_per_sec`,
`min`/`max`/`mean`/`median`/`stdev` and `p50`/`p95`/`p99`) and as a table in `--html-report`. In
distributed mode they are merged across workers like the global stats.

#### Variable extraction & correlation

Steps are not limited to replaying static requests: an `extract` block pulls values out of a
response, and later steps reference them as `${var}`. This is what makes authenticated and
stateful flows testable — login → capture token → call the API with it.

```yaml
name: Login and read profile
on_extract_failure: abort_iteration   # or: continue
on_template_error: abort_iteration    # or: keep_literal
steps:
  - name: login
    method: POST
    path: /auth/login
    body: '{"user": "demo", "pass": "demo"}'
    extract:
      token:
        json: "$.access_token"                 # JSONPath into the JSON body
      session_id:
        header: "X-Session-Id"                 # response header value
      csrf:
        regex: 'name="csrf" value="([^"]+)"'   # first capture group

  - name: get-profile
    path: /me
    headers:
      Authorization: "Bearer ${token}"
      X-Session: "${session_id}"
    assert_status: 200

  - name: submit-form
    method: POST
    path: /form
    body:
      csrf: "${csrf}"
      user_token: "${token}"
```

**Extraction sources** — a rule names exactly one of:

| Source | Expression | Notes |
|--------|-----------|-------|
| `json` | `$.a.b[0].c` | Dotted JSONPath subset: object keys, array indices (negative allowed), and `["quoted keys"]`. Wildcards, slices, filters, and recursive descent are not supported. `$` selects the whole document. |
| `header` | `X-Session-Id` | Response header, matched case-insensitively. |
| `regex` | `value="([^"]+)"` | Searched against the response body; capture group 1 is used. The pattern must have at least one group. |

Non-string JSON values keep their JSON spelling (`true`, not `True`); objects and arrays are
re-serialized compactly, so a whole sub-document can be carried between steps.

**Where `${var}` works:** the step `path`, header names and values, and the `body` — including
inside nested JSON object/array bodies. Values are inserted verbatim, so URL-encode anything that
needs it on the server side. `${...}` is the entire template language: no expressions, no logic.

**Variable scope:** each virtual user has its own variable set, cleared at the start of every
iteration. Users never see each other's tokens, and every iteration starts from the same known
state.

**Failure handling** — both options are scenario-level:

| Option | Values | Behavior |
|--------|--------|----------|
| `on_extract_failure` | `abort_iteration` (default), `continue` | An `extract` rule that produces no value skips the rest of the iteration, or is ignored and the flow continues. |
| `on_template_error` | `abort_iteration` (default), `keep_literal` | A `${var}` that is not bound aborts the iteration, or is sent to the server literally. |

Failures are visible in three places: the `Extract Failures` / `Template Errors` counters in the
terminal summary, the `extract_failures` / `template_errors` fields in JSON output, and the error
distribution as distinct `ExtractFailure: ...` / `TemplateError: ...` keys naming the variable and
the reason. Bad regexes, unsupported JSONPaths, and invalid option values are rejected when the
scenario file loads — not mid-run.

Those dedicated counters record every occurrence, but the headline `Total Errors` (and therefore
`error_rate` thresholds) charges an iteration at most once: a 401, the extraction that failed on
its body, and the `${var}` that could not resolve as a result are one broken flow, not three.

Working example: [`examples/scenario-correlation.json`](examples/scenario-correlation.json).

### HTTP/2

Most production edges (CDNs, ALBs, nginx, Envoy) serve HTTP/2, and their behaviour under load is
qualitatively different: h2 multiplexes streams over one connection instead of holding a
connection per in-flight request. `--http2` generates protocol-representative load against them.

```bash
pip install 'pywrkr[http2]'
pywrkr --http2 -c 100 -d 30 https://edge.example.com/
```

**`-c` means concurrent streams, not connections.** Under HTTP/1.1, `-c 100` opens 100 sockets.
Under HTTP/2 the client multiplexes, so `-c 100` bounds in-flight streams and the socket count is
far lower — worth remembering when comparing an h1 baseline against an h2 run.

**Protocol negotiation is reported, never assumed.** Over `https://`, ALPN decides; a server that
only offers HTTP/1.1 is used as such, counted separately, and warned about, so a run can't quietly
claim to be an HTTP/2 test:

```
  NEGOTIATED PROTOCOL
    HTTP/2:        9,321 (100.0%)
```

JSON output carries the same counts in `http_versions`. Over `http://` there is no ALPN handshake,
so HTTP/2 is used with prior knowledge (h2c) — the only way `--http2` can mean anything against a
cleartext target. A cleartext server that does not speak h2c will fail the requests rather than
silently downgrade.

**`--latency-breakdown` reports less on this backend.** The HTTP/2 client has no hooks for the DNS,
TCP and TLS phases, so those are **omitted** rather than reported as zero — a zero would read as an
impossibly fast connection phase. TTFB, transfer and total are still measured. Connection-reuse
counts are omitted for the same reason: under h2, "200 new connections" would be one connection
carrying 200 streams.

Everything else — virtual users, rate limiting, traffic profiles, scenarios with correlation and
feeders, thresholds, and baseline comparison — works identically on both backends. Distributed
workers must each have the extra installed; a worker without it refuses the run and says so rather
than contributing HTTP/1.1 load to an HTTP/2 result.

### Regression Testing in CI

Absolute gates (`--threshold "p95 < 300ms"`) rot: loose enough never to fire, or tight enough to
flake on infrastructure noise. What a PR gate usually wants is relative — *"fail if p95 got more
than 10% worse than the last known-good run"*:

```bash
# Record a baseline on main
pywrkr --save-baseline baseline.json -c 100 -d 30 https://api.example.com/

# Gate a PR against it, in one command
pywrkr --baseline baseline.json \
       --fail-on "p95 > +10%" --fail-on "rps < -5%" \
       -c 100 -d 30 https://api.example.com/
```

Or compare two existing `--json` files after the fact:

```bash
pywrkr compare baseline.json current.json --fail-on "p95 > +10%"
```

**`--fail-on` expressions** state the condition under which the gate *fails*, and always compare
the **delta**, never the raw value:

| Expression | Fails when |
|------------|-----------|
| `p95 > +10%` | p95 is more than 10% higher than the baseline |
| `rps < -5%` | throughput dropped by more than 5% |
| `p99 > +50ms` | p99 grew by more than 50ms in absolute terms |
| `error_rate > +0.5` | the error rate rose by more than 0.5 **percentage points** |
| `step:checkout.mean > +20ms` | that scenario step's mean latency grew by more than 20ms |

Metrics: `rps`, `error_rate`, `total_requests`, `total_errors`, `total_bytes`, `transfer_rate`,
`duration`, `min_latency`, `max_latency`, `avg_latency`, `median_latency`, `stdev_latency`, any
percentile (`p50`…`p99.99`), and `step:<name>.<field>`. A `%` suffix makes a rule relative;
anything else is an absolute delta in the metric's own unit (`ms`/`us`/`s` accepted for latency).

Note the asymmetry for `error_rate`: `+0.5` is half a percentage point, while `+10%` is 10%
*relative* to the baseline error rate.

**Exit codes:** `0` no regression · `2` an absolute `--threshold` was breached · `3` a `--fail-on`
rule fired · `1` usage or schema error. When both a threshold and a regression fire, `2` wins.

**Output formats:** `--format markdown` produces a table ready to paste into a PR comment;
`--format json` gives a machine-readable verdict. (On the main command the flag is
`--compare-format`.)

**Comparability.** Results carry a `schema_version` and a snapshot of the load shape (mode,
connections, users, duration, host). Comparing a 10-user run against a 1000-user baseline is
arithmetically fine and completely meaningless, so `compare` warns when they differ — and fails
with `--strict-config`.

**Riding out noise.** A single baseline run makes every later run look like a regression when the
baseline happened to be lucky. Point `--baseline` at a glob to average several:

```bash
pywrkr compare 'baselines/*.json' current.json --fail-on "p95 > +10%"
```

A recommended recipe: run 3–5 repetitions, discard the first as warm-up, and keep the rest as the
baseline set.

#### GitHub Action

```yaml
- uses: kurok/pywrkr@v1
  with:
    url: http://localhost:8080/
    args: -c 50 -d 30
    thresholds: |
      p95 < 500ms
      error_rate < 1%
    comment-pr: true
```

That runs the benchmark, gates the job on the thresholds, writes the table to the job summary, and
posts it on the PR — **editing its own previous comment instead of adding a new one**, so a branch
with twenty pushes has one report, not twenty.

Gate on a baseline instead of, or alongside, absolute thresholds:

```yaml
- uses: kurok/pywrkr@v1
  with:
    url: http://localhost:8080/
    args: -c 50 -d 30
    baseline: perf/baseline.json
    fail-on: |
      p95 > +10%
      rps < -5%
    save-baseline: perf/candidate.json
```

| Input | Default | Description |
|-------|---------|-------------|
| `url` | — | Target URL. Omit only if `args` supplies its own target. |
| `args` | `""` | Any other pywrkr flags, e.g. `-c 50 -d 30 --rate 200`. |
| `thresholds` | `""` | Absolute gates, one per line (`p95 < 500ms`). |
| `baseline` | `""` | Results file or glob to compare against. |
| `fail-on` | `""` | Regression rules, one per line (`p95 > +10%`). Requires `baseline`. |
| `save-baseline` | `""` | Also write this run's results here, to commit or cache. |
| `version` | `latest` | Version to install, or `local` for the checked-out tree. |
| `comment-pr` | `false` | Post/update the report on the PR. Needs `pull-requests: write`. |
| `soft-fail` | `false` | Report a breach without failing the step. |
| `html-report` | `""` | Also write a standalone HTML report here. |
| `results-file` | `pywrkr-results.json` | Where the JSON results go. |
| `summary-file` | `pywrkr-report.md` | Where the rendered markdown goes. |
| `job-summary` | `true` | Append the report to the job summary. |
| `title` | `pywrkr performance report` | Heading used in the report. |

Outputs: `passed`, `verdict` (`pass` / `threshold` / `regression` / `error`), `p50`, `p95`, `p99`,
`rps`, `error-rate`, `total-requests`, `results-file`, `summary-file`. Latencies are in seconds and
`error-rate` is a percentage; a metric the run did not produce comes back as an empty string, so a
downstream step can tell "no data" from "zero".

```yaml
- uses: kurok/pywrkr@v1
  id: perf
  with: { url: http://localhost:8080/, args: -c 50 -d 30, soft-fail: "true" }
- run: echo "p95 was ${{ steps.perf.outputs.p95 }}s at ${{ steps.perf.outputs.rps }} req/s"
```

Two behaviours worth knowing about:

- **A threshold on a metric the run never produced fails.** If the target was unreachable there is
  no p95, and a gate that goes green because it found nothing to check is worse than no gate.
- **The action depends on no other action.** It installs pywrkr into a private venv using the
  runner's own Python; there is no `uses:` inside it, so adopting it does not pull anything else
  into your supply chain. Every input reaches the shell through the environment rather than being
  interpolated into a script, so an input from an untrusted fork cannot inject commands.

If you would rather wire it up by hand, `pywrkr summary` is the same code the action calls:

```bash
pywrkr -c 50 -d 30 --json results.json http://localhost:8080/
pywrkr summary results.json \
       --threshold "p95 < 500ms" \
       --baseline perf/baseline.json --fail-on "p95 > +10%" \
       --output report.md --github-output "$GITHUB_OUTPUT"
```

It re-reads the results file rather than re-running anything, and exits `0` / `2` / `3` on the same
rules as the main command.

### WebSocket Benchmarking

Real-time features — chat, live dashboards, trading feeds, collaborative editing — ride on
WebSockets, and what matters about their load is invisible to a request/response benchmarker: a
connection storm, how many sockets a server holds open, how long a message takes to come back on a
socket that is already established. A `ws://` or `wss://` URL switches modes automatically:

```bash
# 500 sockets, each sending a message every second for 60s, measuring round-trip latency
pywrkr wss://ws.example.com/feed -c 500 -d 60 \
       --ws-message '{"op":"ping"}' --ws-message-interval 1 --ws-expect-reply

# Connection storm: open 1000 sockets over 30s and hold them, counting server pushes
pywrkr wss://ws.example.com/feed -c 1000 -d 300 --ramp-up 30
```

`-c` is concurrent sockets, `-d` is how long to hold them, and `--ramp-up` staggers the handshakes
so a connection storm has the shape you asked for instead of arriving all at once.

| Option | Default | Description |
|--------|---------|-------------|
| `--ws-message TEXT` | — | Payload to send; repeat to cycle several. Without any, the run connects and listens |
| `--ws-message-interval S` | `1.0` | Seconds between sends on one socket; `0` sends as fast as the socket allows |
| `--ws-expect-reply` | off | Wait for a reply to each message and report round-trip latency |
| `--ws-reply-timeout S` | `--timeout` | How long to wait for that reply |
| `--ws-subprotocol NAME` | — | `Sec-WebSocket-Protocol` to offer; repeatable |
| `--ws-ping-interval S` | off | Send a ping every S seconds to keep idle sockets alive |
| `--ws-max-message-size B` | 4 MiB | Reject frames larger than this |
| `--ws-close-timeout S` | `5.0` | How long to wait for the peer's close frame |
| `--ws-reconnect` | off | Reopen a socket the server closed instead of leaving the slot empty |
| `--ws-reconnect-delay S` | `1.0` | Pause before reconnecting |

**Which number is the latency?** Stated explicitly rather than left to be inferred, both in the
terminal output and as `websocket.latency_metric` in `--json`:

- With `--ws-expect-reply`, the run's latency — and therefore `--threshold "p95 < 100ms"` — is the
  **message round-trip time**.
- Without it, there is no reply to time, so the latency is the **handshake**.

Handshake and round-trip latency are *also* always reported separately, because a service that
connects instantly and answers slowly and one that does the reverse are different problems that a
single latency line cannot tell apart.

Likewise, `requests_per_sec` counts messages when there are messages to send and connections
otherwise; `websocket.primary_metric` says which.

**What is reported.** On top of the usual percentile/threshold/JSON/HTML machinery, `--json` gains
a `websocket` block:

```json
{
  "websocket": {
    "latency_metric": "rtt",
    "primary_metric": "messages",
    "connections": {"opened": 500, "failed": 0, "dropped": 3, "reconnects": 0,
                    "peak_concurrent": 500},
    "messages": {"sent": 29847, "received": 29844, "sent_per_sec": 497.45,
                 "received_per_sec": 497.40, "bytes_sent": 447705,
                 "bytes_received": 1790640, "reply_timeouts": 3,
                 "unexpected_replies": 0},
    "handshake": {"count": 500, "min": 0.0012, "max": 0.041, "mean": 0.0089,
                  "percentiles": {"p50": 0.0081, "p95": 0.0223, "p99": 0.0388}},
    "rtt":       {"count": 29844, "min": 0.0004, "max": 0.112, "mean": 0.0021,
                  "percentiles": {"p50": 0.0018, "p95": 0.0044, "p99": 0.0091}},
    "close": {"frames_sent": 500, "unacknowledged": 0, "codes": {"1000": 500}}
  }
}
```

`close.unacknowledged` counts sockets whose close frame the server never answered — a server that
does not read its sockets shows up here instead of as a silent zero.

**Clean shutdown.** Every socket is closed with a close frame, on normal completion and on
`Ctrl-C` alike, so a benchmark does not leave the server holding thousands of half-open connections
that poison whatever you measure next. That teardown is deliberately excluded from the reported
duration: waiting on an unresponsive peer is not load, and counting it would deflate every rate
derived from it.

`wss://` uses the same TLS settings as `https://` — `--ssl-verify` and `--ca-bundle` behave
identically.

#### Mixed HTTP + WebSocket scenarios

A `ws:` step in a scenario opens a socket on the *same session* as the HTTP steps around it, so it
inherits their cookies, and `${var}` correlation works across the protocol boundary:

```json
{
  "steps": [
    {"name": "login", "path": "/api/login", "method": "POST",
     "extract": {"token": {"json": "$.token"}}},
    {"name": "subscribe", "ws": "wss://ws.example.com/feed?auth=${token}",
     "send": "{\"op\":\"subscribe\",\"channel\":\"orders\"}",
     "expect_message_contains": "\"subscribed\"",
     "hold": "30s",
     "extract": {"sid": {"json": "$.sid"}}}
  ]
}
```

| Key | Description |
|-----|-------------|
| `ws` | The `ws://`/`wss://` URL. Absolute — `base_url` is not prepended. Templated. |
| `send` | Payload to send once the socket is open. Templated. |
| `expect_message_contains` | Wait for a message containing this substring. Scans every arriving message, not just the first, so a confirmation behind a welcome frame or a heartbeat still matches. Its text is what the step's `extract` rules run against. |
| `hold` | Keep the socket open afterwards (`"30s"`, `"250ms"`, or a bare number of seconds), counting what the server pushes. |

The step's latency is the whole thing — handshake, send, and the wait for the expected message —
because that is what a user of a "subscribe and get the first update" flow actually waits for.
`hold` afterwards is passive listening and is not counted in it. HTTP-only keys (`method`, `body`,
`assert_status`) are rejected on a `ws:` step rather than silently ignored.

See [`examples/scenario-websocket.json`](examples/scenario-websocket.json).

**Not supported yet:** distributed WebSocket mode (`--master` rejects a `ws://` target; a mixed
HTTP/WebSocket *scenario* does run distributed), Socket.IO/SockJS protocol layers, and gRPC/SSE.

### Data-Driven Testing

Identical payloads systematically overstate cache performance and understate database and
session-store load. A scenario can declare named **data sets** so every virtual user works from
its own row:

```yaml
data:
  users:
    file: users.csv        # or users.json (a list of flat objects)
    strategy: unique       # loop | sequential | random | unique
steps:
  - name: login
    method: POST
    path: /auth/login
    body: '{"user": "${users.username}", "pass": "${users.password}"}'
```

```bash
pywrkr --scenario examples/scenario-data-driven.json -u 100 -d 60

# Or attach a data set from the CLI, without touching the scenario file:
pywrkr --scenario flow.json --data users=users.csv --data-strategy users=unique -u 100 -d 60
```

Each user draws one row per data set at the **start of every iteration** and references its
columns as `${dataset.column}`, anywhere templating works — path, headers, and body.

**Strategies** — the cursor is shared by all users in a run, so `unique` really is unique rather
than unique-per-user:

| Strategy | Behavior |
|----------|----------|
| `loop` (default) | Rows handed out round-robin, wrapping around forever |
| `sequential` | Like `loop`, but users stop when the rows run out |
| `random` | A uniformly random row per iteration, with replacement |
| `unique` | Each row used at most once for the whole run; users stop when spent |

`unique` is checked **before the run starts**: if there are fewer rows than the load needs — one
per virtual user, and one per iteration when `-n` fixes the request count — pywrkr refuses to
start rather than quietly running short. In distributed mode the master hands each worker a
disjoint slice of the rows, so `unique` and `sequential` stay globally unique across nodes.

**File format.** CSV needs a header row, which supplies the field names; values are strings. JSON
must be an array of flat objects; scalars keep their JSON spelling (`true`, not `True`). Relative
`file:` paths resolve against the scenario file's own directory, so a scenario and its data travel
together. Rows are read into memory once at startup — fine for the hundreds-of-thousands range,
but streaming very large files is deliberately not supported.

### Built-in Template Functions

Available anywhere `${...}` works, with no data file needed:

| Function | Expands to |
|----------|-----------|
| `${uuid()}` | A random UUID4 |
| `${randint(1,100)}` | A random integer in the inclusive range |
| `${randstr(12)}` | A random alphanumeric string of that length |
| `${counter()}` / `${counter(name)}` | A run-wide counter starting at 1; named counters are independent |
| `${now()}` / `${now(unix)}` | ISO 8601 UTC timestamp / epoch seconds |

```json
{ "reference": "order-${counter(orders)}", "id": "${uuid()}", "placed_at": "${now()}" }
```

Counters are shared across virtual users, so `counter()` is strictly monotonic for the run rather
than restarting per user. Unknown functions and nonsense arguments (`${randint(9,1)}`) are
rejected when the scenario file loads, naming the step — not once per request mid-run.

There is deliberately no expression language: no arithmetic, no conditionals, no nesting.

Working example: [`examples/scenario-data-driven.json`](examples/scenario-data-driven.json) with
[`examples/users.csv`](examples/users.csv).

### Sessions & Cookies

In user-simulation and scenario modes every virtual user gets **its own cookie jar**, so
`Set-Cookie` is stored and replayed for that user across steps and iterations. Cookie-session
logins — the most common form of web auth — work without any correlation setup:

```bash
# The server sets a session cookie on /login; each user carries its own from then on
pywrkr --scenario examples/scenario-cookie-session.json -u 100 -d 60
```

This also means N virtual users look like N distinct clients to the target, which matters for
anything keyed on identity: session-store load, sticky-session balancing, per-user rate limits,
and cache hit rates.

| Setting | Where | Effect |
|---------|-------|--------|
| default | — | one cookie jar per virtual user, kept for the whole run |
| `session: fresh_per_iteration` | scenario file | empty the jar at the start of each iteration, so every pass is a brand-new visitor |
| `--no-session-cookies` | CLI | ignore `Set-Cookie` entirely; only the static `-C` cookies are sent |

**Static `-C` cookies** are sent on every request in every mode. They travel in the request's
`Cookie` header rather than the jar, so they survive `session: fresh_per_iteration` and are
unaffected by `--no-session-cookies`. A server-set cookie of the same name is sent alongside them.

**IP-address targets:** cookie jars normally refuse to store cookies for a bare IP host, which
would silently disable sessions against the `http://127.0.0.1:8080` targets load tests usually
point at. pywrkr detects an IP literal in the target URL and opens the jar (`unsafe`) for it, so
loopback and internal-IP targets behave like named hosts.

**Plain connection mode** (`-c`/`-d`, no `-u`) is unchanged: there is no per-user identity to
isolate, so it keeps the client library's default jar. `--no-session-cookies` still applies.

**Distributed mode:** jars live per virtual user inside each worker process. There is no shared
session state between worker nodes, so a session started on one node is never continued on another.

### Cache-Busting Mode

Append `-R` to any mode to bypass HTTP caches by adding a unique query parameter to each request:

```bash
pywrkr -R -u 300 -d 120 https://example.com/
# Each request hits: https://example.com/?_cb=<unique-uuid>
```

This is useful for testing origin server performance without CDN/proxy cache interference.

### Rate Limiting Mode

Instead of sending requests as fast as possible, `--rate` sends them at a controlled, constant rate. This is critical for SLA testing and finding exact server breaking points.

```bash
# Constant 500 req/s for 30 seconds
pywrkr --rate 500 -d 30 http://localhost:8080/

# Rate with request count: 50 req/s, stop after 200 requests
pywrkr --rate 50 -n 200 http://localhost:8080/

# Rate limiting with multiple connections (rate is global, shared across all workers)
pywrkr --rate 100 -c 10 -d 60 http://localhost:8080/

# Combine with user simulation (applies when think_time is 0)
pywrkr --rate 200 -u 50 -d 120 --think-time 0 http://localhost:8080/
```

**Rate Ramp** (`--rate-ramp`): Linearly increase the rate over the test duration. This is useful for finding the exact breaking point automatically:

```bash
# Start at 100 req/s, linearly increase to 1000 req/s over 60 seconds
pywrkr --rate 100 --rate-ramp 1000 -d 60 http://localhost:8080/
```

At `--rate 500`, the tool sends one request every 2ms. If the server cannot keep up (latency exceeds the interval), requests queue up -- this is expected and useful for identifying saturation points.

**Comparison with default "max throughput" mode:**

| Mode | Use Case |
|------|----------|
| Default (no `--rate`) | Find maximum throughput; stress test |
| `--rate N` | SLA validation; controlled load; latency-under-load testing |
| `--rate N --rate-ramp M` | Find breaking point; gradual load increase |
| `--rate N --traffic-profile P` | Realistic traffic patterns (sine, spikes, CSV replay) |

Results include "Target RPS" vs "Actual RPS" and "Rate Limit Waits" count (how many times the limiter had to slow down a worker).

### Traffic Profiles

Shape your test traffic to match real-world patterns using `--traffic-profile`. Requires `--rate` (base/peak rate) and `-d` (duration).

```bash
# Sine wave: smooth oscillation up to 1000 req/s, 3 cycles
pywrkr --rate 1000 -d 120 --traffic-profile "sine:cycles=3,min=0.2" http://localhost:8080/

# Step function: jump between discrete load levels
pywrkr --rate 1000 -d 90 --traffic-profile "step:levels=100,500,1000" http://localhost:8080/

# Spike: baseline at 20% with 5x bursts every 10 seconds
pywrkr --rate 200 -d 60 --traffic-profile "spike:interval=10,multiplier=5" http://localhost:8080/

# Business hours: 24h daily pattern compressed into test duration
pywrkr --rate 2000 -d 300 --traffic-profile business-hours http://localhost:8080/

# CSV replay: replay real production traffic from a file
pywrkr --rate 1000 -d 300 --traffic-profile "csv:traffic.csv" http://localhost:8080/
```

**Built-in profiles:**

| Profile | Pattern | Use case |
|---------|---------|----------|
| `sine` | Smooth wave | Gradual load changes, auto-scaling tests |
| `step` | Discrete jumps | Testing specific load tiers |
| `sawtooth` | Repeated ramps | Repeated warm-up behavior |
| `square` | On/off toggle | Sudden load change recovery |
| `spike` | Periodic bursts | Flash sale / viral event simulation |
| `business-hours` | Day/night curve | Realistic daily traffic patterns |
| `csv:file` | Custom curve | Replaying real production traffic |

**CSV format:** Two columns — `time_sec,rate` (absolute RPS) or `time_sec,multiplier` (factor applied to `--rate`). Values are linearly interpolated between points.

### Latency Breakdown

Use `--latency-breakdown` to see where each request spends its time. This breaks down latency into individual phases using aiohttp's tracing infrastructure:

```bash
# Show latency breakdown for each phase
pywrkr --latency-breakdown -n 1000 -c 50 https://example.com/

# Combine with JSON output
pywrkr --latency-breakdown --json results.json -d 30 https://example.com/
```

Output includes averages with min/max/p50/p95 for each phase:

```
======================================================================
  LATENCY BREAKDOWN (averages)
======================================================================
    DNS Lookup:          2.15ms  (min=1.20ms, max=5.30ms, p50=2.00ms, p95=4.10ms)
    TCP Connect:        12.34ms  (min=10.00ms, max=18.50ms, p50=12.00ms, p95=16.20ms)
    TLS Handshake:      45.67ms  (min=40.00ms, max=55.00ms, p50=45.00ms, p95=52.00ms)
    TTFB:               89.12ms  (min=60.00ms, max=150.00ms, p50=85.00ms, p95=130.00ms)
    Transfer:           34.56ms  (min=20.00ms, max=80.00ms, p50=30.00ms, p95=65.00ms)
    Total:             183.84ms  (min=131.20ms, max=308.80ms, p50=174.00ms, p95=267.30ms)

    New Connections:    50
    Reused Connections: 950
```

**Phases:**
- **DNS Lookup** -- Time to resolve the hostname via DNS
- **TCP Connect** -- Time to establish the TCP connection
- **TLS Handshake** -- Time for TLS negotiation (HTTPS only)
- **TTFB** -- Time to first byte, from sending the request to receiving the first response byte
- **Transfer** -- Time to read the full response body

**Connection reuse:** When keep-alive is enabled (the default), most requests reuse existing connections. For reused connections, DNS/Connect/TLS phases will be zero. The breakdown reports how many connections were new vs. reused.

When `--json` is used, the breakdown data is included in the JSON output under the `latency_breakdown` key.

### Auto-Ramping / Step Load (Autofind)

Automatically increase load until the server's capacity is found. The `--autofind` flag starts with a small number of users, runs short tests at increasing load levels, and uses binary search to pinpoint the maximum sustainable load.

```bash
# Find max capacity with default thresholds (1% error rate, 5s p95)
pywrkr --autofind https://example.com/

# Custom thresholds: 0.5% error rate, 2s p95, 15s steps
pywrkr --autofind --max-error-rate 0.5 --max-p95 2.0 \
    --step-duration 15 https://example.com/

# Start from 50 users, up to 5000, multiply by 1.5x each step
pywrkr --autofind --start-users 50 --max-users 5000 \
    --step-multiplier 1.5 https://example.com/

# Save detailed results to JSON
pywrkr --autofind --json autofind_results.json https://example.com/

# With cache-busting and custom think time
pywrkr --autofind -R --think-time 0.5 https://example.com/
```

**How it works:**

1. Start with `--start-users` (default: 10) virtual users
2. Run a short test (`--step-duration`, default: 30s) at that load
3. Check if error rate exceeds `--max-error-rate` or p95 latency exceeds `--max-p95`
4. If OK, multiply users by `--step-multiplier` (default: 2x) and repeat
5. If thresholds exceeded, binary search between the last good and first bad user count
6. Report the maximum sustainable load with a summary table

**Example output:**

```
============================================================
  AUTOFIND RESULTS
============================================================
  Maximum sustainable load: 280 users

  Step Results:
  Users |      RPS |     p50 |     p95 |     p99 | Errors | Status
     10 |      9.8 |   120ms |   180ms |   200ms |   0.0% | OK
     20 |     19.5 |   125ms |   190ms |   220ms |   0.0% | OK
     40 |     38.2 |   130ms |   250ms |   300ms |   0.0% | OK
     80 |     75.1 |   180ms |   400ms |   600ms |   0.0% | OK
    160 |    140.2 |   350ms |    1.2s |    2.1s |   0.0% | OK
    320 |    135.5 |    2.1s |    8.5s |   15.2s |   5.2% | FAIL
    240 |    138.1 |   800ms |    3.2s |    5.1s |   0.8% | OK
    280 |    136.8 |    1.1s |    4.8s |    7.2s |   0.9% | OK
    300 |    135.2 |    1.5s |    5.5s |    9.1s |   1.2% | FAIL
============================================================
```

**Autofind options:**

| Flag | Description |
|------|-------------|
| `--autofind` | Enable auto-ramping mode |
| `--max-error-rate` | Stop when error rate exceeds this percent (default: 1.0) |
| `--max-p95` | Stop when p95 latency exceeds this in seconds (default: 5.0) |
| `--step-duration` | Duration of each step test in seconds (default: 30) |
| `--start-users` | Starting number of users (default: 10) |
| `--max-users` | Maximum users to try (default: 10000) |
| `--step-multiplier` | Multiply users by this each step (default: 2.0) |

### SLO-Aware Thresholds

Define pass/fail criteria for your benchmarks. If any threshold is breached, pywrkr exits with code 2 — making it usable in CI/CD pipelines.

```bash
# Single threshold
pywrkr --threshold "p95 < 300ms" -c 100 -d 30 http://localhost:8080/

# Multiple thresholds
pywrkr \
    --th "p95 < 300ms" \
    --th "p99 < 1s" \
    --th "error_rate < 1%" \
    --th "rps > 100" \
    -c 100 -d 30 http://localhost:8080/
```

**Supported metrics:**
- `p50`, `p75`, `p90`, `p95`, `p99` — latency percentiles
- `avg_latency`, `max_latency`, `min_latency` — latency aggregates
- `error_rate` — error percentage (e.g., `error_rate < 1%` or `error_rate < 1`)
- `rps` — requests per second

**Operators:** `<`, `>`, `<=`, `>=`

**Time units:** `ms` (milliseconds), `s` (seconds), `us` (microseconds). Default is seconds if no unit.

**Example output:**
```
======================================================================
  SLO THRESHOLDS
======================================================================
    p95 < 300ms         Actual: 245.00ms       PASS
    p99 < 1s            Actual: 820.00ms       PASS
    error_rate < 1%     Actual: 0.00%          PASS
    rps > 100           Actual: 523.45         PASS

  Result: ALL THRESHOLDS PASSED
```

**CI usage:**
```bash
pywrkr --th "p95 < 500ms" --th "error_rate < 0.1%" \
    -c 50 -d 60 http://api.staging/health || echo "Performance regression detected!"
```

### Observability Export

Export benchmark metrics directly to your observability stack.

#### OpenTelemetry

```bash
pip install pywrkr[otel]
pywrkr --otel-endpoint http://localhost:4318 \
    --tag environment=staging --tag build=$(git rev-parse --short HEAD) \
    -c 100 -d 30 http://localhost:8080/
```

Exports gauges and counters: `pywrkr.requests.total`, `pywrkr.errors.total`, `pywrkr.requests_per_sec`, `pywrkr.latency.p50/p95/p99/mean/max`, `pywrkr.transfer_bytes_per_sec`, `pywrkr.duration_sec`.

#### Prometheus Remote Write (Pushgateway)

```bash
pywrkr --prom-remote-write http://pushgateway:9091 \
    --tag region=us-east-1 --tag service=api \
    -c 100 -d 30 http://localhost:8080/
```

Uses stdlib `urllib` — no extra dependencies. Pushes metrics in Prometheus text format to `{endpoint}/metrics/job/pywrkr`.

#### Live streaming during the run (`--export-interval`)

By default metrics are pushed **once, at the end**. For the runs where observability matters most
— a 30-minute soak, an autofind ramp, a traffic profile — that leaves you blind until it's over,
and a killed run exports nothing at all. `--export-interval` streams snapshots as the run happens:

```bash
pywrkr --otel-endpoint http://collector:4318 --export-interval 10 \
    --tag test=soak-v2 --rate 500 -d 1800 https://api.example.com/
```

**Counters stay cumulative** (`pywrkr_requests_total`, `pywrkr_errors_total`), so Prometheus
`rate()` and OTel deltas work normally. **Percentiles are windowed** — computed over the last
interval only, so a spike 25 minutes ago is not still dragging your current p95 around. The
run-cumulative percentiles are still what the end-of-run export carries.

Each streamed snapshot is labelled `export="interval"`, and the one emitted at shutdown
`export="final"`, so a dashboard can separate live points from the closing state. **A run
interrupted with Ctrl-C still emits that final snapshot**, so an aborted soak leaves its last
state in the TSDB rather than a cliff.

**A slow collector never slows the run.** Sampling and sending are separate tasks joined by a
bounded queue: if the endpoint is unreachable, snapshots are dropped rather than backing up into
the request path, and the count is reported at the end —

```
  Streaming export: 0 snapshot(s) exported, 3 never delivered (collector unresponsive)
```

— never a silent success. Measured cost against an unreachable collector: ~1% of throughput.

Without `--export-interval` nothing changes: one export at the end, exactly as before.

**Grafana walkthrough.** Point pywrkr at a collector that writes to your Prometheus, then graph:

| Panel | Query |
|-------|-------|
| Achieved throughput | `rate(pywrkr_requests_total[1m])` |
| Error rate | `rate(pywrkr_errors_total[1m]) / rate(pywrkr_requests_total[1m])` |
| Windowed p95 | `pywrkr_latency_p95_ms{export="interval"}` |
| Target vs achieved | `pywrkr_requests_per_sec` against your `--rate` |

Put those beside your service's own dashboards and a latency spike lines up with the deploy, GC
pause, or scaling event that caused it — while the test is still running. With `--autofind`, every
snapshot carries a `step_users` label, so each step of the ramp is a separable series instead of
one smeared line.

#### Test Metadata Tags

Tags are attached to all exported metrics and included in JSON output:

```bash
pywrkr --tag environment=production --tag build=v2.1.0 \
    --tag region=eu-west-1 --tag test_name=api_stress \
    --json results.json -c 100 -d 30 http://localhost:8080/
```

### Multi-URL Mode

Test multiple endpoints in a single benchmark run using a URL file:

```bash
# Create a URL file (one URL per line)
cat urls.txt
http://localhost:8080/api/users
http://localhost:8080/api/products
http://localhost:8080/api/orders

# Run benchmark against all URLs
pywrkr --url-file urls.txt -c 50 -d 30
```

| Flag | Description |
|------|-------------|
| `--url-file` | Path to file containing URLs to test (one per line) |

Requests are distributed across all URLs. Results include per-URL breakdowns alongside aggregate statistics.

### Distributed Mode

Scale benchmarks across multiple machines by running one master and multiple workers:

```bash
# On the master node: coordinate 3 workers
pywrkr http://target:8080/ --master --expect-workers 3 -c 300 -d 60

# On each worker node: connect back to the master
pywrkr --worker master-host:9220
```

| Flag | Description |
|------|-------------|
| `--master` | Run as distributed master (coordinates workers) |
| `--worker HOST:PORT` | Run as distributed worker, connecting to master at HOST:PORT |
| `--expect-workers` | Number of workers the master should wait for before starting |
| `--bind` | Master bind address (default: `0.0.0.0`) |
| `--port` | Master listen port (default: `9220`) |

The master splits the workload evenly across workers, collects results, and produces a single aggregated report.

### TLS / SSL Verification

By default, SSL certificate verification is **disabled** to allow benchmarking dev/staging servers with self-signed certs. Enable it for production targets and supply a custom CA bundle when needed:

```bash
# Enable standard TLS verification
pywrkr https://example.com --ssl-verify -c 50 -d 30

# Benchmark an internal HTTPS service with a corporate CA
pywrkr https://internal.corp.example.com/api/health \
  --ssl-verify \
  --ca-bundle /etc/ssl/certs/corporate-ca.pem \
  --duration 60
```

The same options are available via environment variables: `PYWRKR_SSL_VERIFY=true` and `PYWRKR_CA_BUNDLE=/path/to/ca.pem`.

## Installation

```bash
# Basic (aiohttp only)
pip install pywrkr

# With live TUI dashboard
pip install pywrkr[tui]

# With OpenTelemetry export
pip install pywrkr[otel]

# Everything
pip install pywrkr[all]
```

## Development Setup

```bash
# Install in editable mode with dev + lint dependencies
pip install -e ".[dev,lint]"
```

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Run a specific test file
python -m pytest tests/test_pywrkr.py -v
python -m pytest tests/test_har_import.py -v

# Run a specific test class
python -m pytest tests/test_pywrkr.py::TestMakeUrl -v

# Run tests sequentially (useful for debugging)
python -m pytest tests/ -v -n 0
```

The test suite includes unit and integration tests covering:
- Formatting helpers, percentiles, histogram, timeline, CSV/JSON/HTML output
- Integration tests with a real aiohttp test server (duration mode, request-count mode, POST, auth, cookies, content-length verification, keepalive, cache-buster)
- User simulation integration tests (think time, ramp-up, jitter, error handling, output formats)
- Autofind integration tests (healthy server, error endpoint, threshold enforcement, binary search, JSON output, summary table)
- HAR import tests (parsing, filtering, scenario generation)
- Reporting module tests (formatting, percentile computation, threshold evaluation, CSV/JSON output)
- Multi-URL mode tests (URL file loading, entry parsing)
- Distributed mode tests (config/stats serialization, merge operations, TCP protocol)
- Worker utility tests (URL construction, headers, stats merging, breakdown aggregation)

## Contributing

Contributions are welcome! Please read the [Contributing Guide](CONTRIBUTING.md) for details on how to get started, report bugs, suggest features, and submit pull requests.

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## License

MIT
