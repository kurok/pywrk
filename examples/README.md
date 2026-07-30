# pywrkr Examples

Sample benchmark outputs generated against `https://example.com/`. These demonstrate all major output formats and modes.

## Quick Reference

### 1. Basic Benchmark (JSON + CSV + HTML Report)

```bash
pywrkr -c 10 -d 5 \
    --json examples/basic-benchmark.json \
    -e examples/percentiles.csv \
    --html-report examples/report.html \
    https://example.com/
```

**Output files:**
- [`basic-benchmark-output.txt`](basic-benchmark-output.txt) — terminal output
- [`basic-benchmark.json`](basic-benchmark.json) — structured JSON results
- [`percentiles.csv`](percentiles.csv) — latency percentile table
- [`report.html`](report.html) — interactive Gatling-style HTML report (open in browser)

### 2. Rate-Limited Benchmark

Send requests at a controlled, constant rate:

```bash
pywrkr --rate 50 -d 5 \
    --json examples/rate-limited.json \
    https://example.com/
```

**Output files:**
- [`rate-limited-output.txt`](rate-limited-output.txt) — terminal output with Target RPS and Rate Limit Waits
- [`rate-limited.json`](rate-limited.json) — JSON results

### 3. Traffic Profile (Sine Wave)

Shape traffic as a sine wave oscillating between 20% and 100% of base rate:

```bash
pywrkr --rate 50 -d 5 \
    --traffic-profile "sine:cycles=2,min=0.2" \
    --json examples/traffic-profile-sine.json \
    https://example.com/
```

**Output files:**
- [`traffic-profile-sine-output.txt`](traffic-profile-sine-output.txt) — terminal output showing traffic profile info
- [`traffic-profile-sine.json`](traffic-profile-sine.json) — JSON results with `traffic_profile` field

### 4. User Simulation

Simulate 5 virtual users with think time and gradual ramp-up:

```bash
pywrkr -u 5 -d 5 \
    --think-time 0.5 --ramp-up 2 \
    --json examples/user-simulation.json \
    https://example.com/
```

**Output files:**
- [`user-simulation-output.txt`](user-simulation-output.txt) — terminal output with per-user stats
- [`user-simulation.json`](user-simulation.json) — JSON results

### 5. Latency Breakdown

See where each request spends its time (DNS, TCP, TLS, TTFB, transfer):

```bash
pywrkr -c 5 -d 5 \
    --latency-breakdown \
    --json examples/latency-breakdown.json \
    https://example.com/
```

**Output files:**
- [`latency-breakdown-output.txt`](latency-breakdown-output.txt) — terminal output with per-phase breakdown
- [`latency-breakdown.json`](latency-breakdown.json) — JSON results with `latency_breakdown` object

### 6. SLO Threshold Checks

Validate that latency and error rates meet your SLOs:

```bash
pywrkr -c 5 -d 5 \
    --threshold "p95 < 500ms" \
    --threshold "error_rate < 5%" \
    --json examples/threshold-check.json \
    https://example.com/
```

**Output files:**
- [`threshold-check-output.txt`](threshold-check-output.txt) — terminal output with PASS/FAIL results
- [`threshold-check.json`](threshold-check.json) — JSON results

Exit code is `0` if all thresholds pass, `2` if any breach.

### 7. HAR Import (Browser Recording)

Convert a browser-recorded HAR file into a pywrkr scenario or URL list:

```bash
# Convert HAR to scenario JSON (default: filters static assets, derives think times):
pywrkr har-import examples/sample-recording.har -o examples/har-import-scenario.json

# Convert HAR to URL file for --url-file mode:
pywrkr har-import examples/sample-recording.har --format url-file -o examples/har-import-urls.txt

# Include static assets and add status assertions:
pywrkr har-import examples/sample-recording.har --include-static --assert-status \
    -o examples/har-import-scenario-with-assertions.json

# Then run the generated scenario:
pywrkr --scenario examples/har-import-scenario.json -u 50 -d 30 https://api.example.com
```

**Input file:**
- [`sample-recording.har`](sample-recording.har) — sample HAR file with GET, POST, PUT, DELETE requests plus static assets

**Output files:**
- [`har-import-scenario.json`](har-import-scenario.json) — generated scenario (API requests only, with think times)
- [`har-import-urls.txt`](har-import-urls.txt) — generated URL file
- [`har-import-scenario-with-assertions.json`](har-import-scenario-with-assertions.json) — scenario with static assets and status assertions

### 8. Scripted Scenario with Correlation

Replay a multi-step authenticated flow: log in, capture the token / user id / session header, then
use them in later steps. Values extracted from one response become `${var}` in the next request's
path, headers, or body.

```bash
# Run the login flow with 100 virtual users for 60 seconds:
pywrkr --scenario examples/scenario-correlation.json -u 100 -d 60

# The scenario carries its own base_url; override it with a positional URL:
pywrkr --scenario examples/scenario-correlation.json -u 50 -d 30 https://staging.example.com
```

**Input file:**
- [`scenario-correlation.json`](scenario-correlation.json) — login → profile → form → save → logout,
  using all three extraction sources (`json`, `header`, `regex`)

Extraction failures and unresolved `${var}` references show up as `Extract Failures` /
`Template Errors` in the terminal summary, as `extract_failures` / `template_errors` in JSON output,
and as distinct `ExtractFailure: ...` / `TemplateError: ...` keys in the error distribution.

### 9. Cookie-Session Login (per-user sessions)

No token juggling required: the server sets a session cookie on login and each virtual user keeps
its own jar, so the rest of the flow is authenticated automatically. Only the CSRF token — which
lives in the HTML, not a cookie — needs extracting.

```bash
# 100 users each logging in as their own session
pywrkr --scenario examples/scenario-cookie-session.json -u 100 -d 60

# Every iteration as a brand-new visitor: add "session": "fresh_per_iteration" to the file
# Ignore Set-Cookie entirely (e.g. benchmarking a CDN, where per-user cookies fragment the cache):
pywrkr --scenario examples/scenario-cookie-session.json -u 100 -d 60 --no-session-cookies
```

**Input file:**
- [`scenario-cookie-session.json`](scenario-cookie-session.json) — CSRF extract → form login → two
  authenticated pages → logout

Static `-C` cookies are always sent on top of the jar, in every mode.

### 10. Data-Driven Scenario (CSV feeder + generators)

Log in as a different user on every iteration, driven by a CSV, with generated values for the
parts that should be unique per request:

```bash
# 5 users, each consuming one distinct CSV row (strategy: unique)
pywrkr --scenario examples/scenario-data-driven.json -u 5 -d 30

# Attach the data set from the CLI instead of the scenario file:
pywrkr --scenario flow.json --data users=examples/users.csv --data-strategy users=loop -u 100 -d 60
```

**Input files:**
- [`users.csv`](users.csv) — 5 credentials with `plan` and `region` columns
- [`scenario-data-driven.json`](scenario-data-driven.json) — login → search → create-order, using
  `${users.*}` columns plus `${uuid()}`, `${randstr()}`, `${randint()}`, `${counter(orders)}`, and
  `${now()}`

With `strategy: unique` each row is used at most once for the whole run and users stop once the
rows are spent, so the run size is bounded by the data. `loop` wraps around instead.

## Other Traffic Profiles

```bash
# Step function: discrete load levels
pywrkr --rate 100 -d 30 --traffic-profile "step:levels=20,50,100" https://example.com/

# Spike: periodic bursts at 5x baseline
pywrkr --rate 50 -d 30 --traffic-profile "spike:interval=10,multiplier=5" https://example.com/

# Square wave: alternating high/low
pywrkr --rate 50 -d 30 --traffic-profile "square:cycles=3,low=0.1" https://example.com/

# Sawtooth: repeated ramps
pywrkr --rate 50 -d 30 --traffic-profile "sawtooth:cycles=3" https://example.com/

# Business hours: 24h pattern compressed into test duration
pywrkr --rate 100 -d 60 --traffic-profile business-hours https://example.com/

# CSV replay: custom traffic curve from file
pywrkr --rate 100 -d 60 --traffic-profile "csv:traffic.csv" https://example.com/
```

## Sample CSV Traffic File

Create `traffic.csv` for CSV replay:

```csv
time_sec,rate
0,10
15,50
30,100
45,50
60,10
```

Or use multiplier mode:

```csv
time_sec,multiplier
0,0.1
15,0.5
30,1.0
45,0.5
60,0.1
```
