# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **HTTP/2 support** via a pluggable client backend (`--http2`, `pip install 'pywrkr[http2]'`). The worker loop, stats and reporting now talk to a `Backend`/`BackendSession` interface with two implementations: aiohttp (default, unchanged) and httpx (HTTP/2). Every mode works on both — duration, `-n`, `-u`, `--rate`, traffic profiles, scenarios with correlation and feeders, thresholds, and baseline comparison. The abstraction also leaves room for an HTTP/3 backend without another refactor. (#189)
- The negotiated protocol is reported rather than assumed: a `NEGOTIATED PROTOCOL` section in the terminal summary, `http_versions` counts in JSON output, and a loud warning when `--http2` fell back to HTTP/1.1 because the server only offered it. Over `https://` the protocol comes from ALPN; over `http://` HTTP/2 is used with prior knowledge (h2c). Note that `-c` bounds concurrent *streams* under HTTP/2, not connections. (#189)
- `--latency-breakdown --http2` degrades honestly: the HTTP/2 backend has no hooks for the DNS, TCP and TLS phases, so those are **omitted** from the breakdown rather than reported as zero, and connection-reuse counts are omitted too (under h2 they would describe one connection carrying many streams). `LatencyBreakdown` gained an `available` field to carry which phases a sample actually measured. (#189)
- Distributed workers honor `--http2`; a worker without the extra installed refuses the run and reports why, instead of quietly contributing HTTP/1.1 load to a result labelled HTTP/2. (#189)

- **Baseline comparison & regression detection**: `pywrkr compare baseline.json current.json --fail-on "p95 > +10%"` diffs two `--json` result files and gates on the deltas, so a CI job can fail on *relative* change instead of an absolute threshold that is either too loose to fire or too tight not to flake. `--fail-on` rules accept relative (`+10%`) or absolute (`+50ms`, `+0.5`) deltas in either direction, over every metric in the JSON schema — percentiles, `rps`, `error_rate`, latency stats, transfer rate, and per-step metrics as `step:<name>.<field>`. Output as a human table (default), `--format markdown` for pasting into a PR comment, or `--format json` for a machine-readable verdict. Exit codes: 0 no regression, 3 a rule fired, 1 usage/schema error. (#191)
- The same gate runs inline on the main command via `--baseline` / `--fail-on` / `--save-baseline`, turning a three-step CI recipe into one invocation. An absolute `--threshold` breach (exit 2) keeps precedence over a relative regression (exit 3). Works in distributed mode too, applied to the merged cluster result rather than per worker. (#191)
- JSON output gains `schema_version` and a `config` snapshot (mode, connections, users, duration, request count, rate, target host). `compare` warns when two runs used different load shapes — comparing a 10-user run to a 1000-user baseline is meaningless — and fails on the mismatch with `--strict-config`. Files written before `schema_version` existed are read as version 1. (#191)
- `--baseline` accepts a glob (`'baselines/*.json'`) and averages the runs it matches, which is the cheap defence against a single unlucky baseline making every later run look like a regression. (#191)

- **Data-driven testing**: a scenario's `data` block (or `--data NAME=FILE`) declares named CSV/JSON data sets, and each virtual user draws one row per set at the start of every iteration, referenced as `${dataset.column}` in the path, headers, or body. Strategies `loop` (default), `sequential`, `random`, and `unique` are selected per set via `strategy:` or `--data-strategy NAME=STRATEGY`. Row cursors are shared across users, so `unique` is unique for the whole run rather than per user; in distributed mode the master hands each worker a disjoint slice so that still holds across nodes. `unique` capacity is checked before the run starts, so a data set too small for the requested load is a startup error instead of a quietly short run. (#187)
- **Built-in template functions** usable anywhere `${...}` works: `${uuid()}`, `${randint(lo,hi)}`, `${randstr(n)}`, `${counter()}` / `${counter(name)}`, and `${now()}` / `${now(unix)}`. Counters are run-wide and strictly monotonic. Unknown functions, bad arities, and nonsense arguments (`${randint(9,1)}`) are rejected when the scenario file loads, naming the step — as are `${dataset.field}` references to a field the data file does not have. No new runtime dependencies. (#187)

- **Per-VU cookie sessions**: in user-simulation and scenario modes each virtual user keeps its own cookie jar, so `Set-Cookie` is stored and replayed for that user across steps and iterations — cookie-session logins are now testable, and N virtual users present as N distinct clients instead of one anonymous loop. Because a cookie jar normally refuses to store cookies for a bare IP host, pywrkr detects an IP literal in the target URL and opens the jar for it, so `http://127.0.0.1:8080` targets behave like named hosts. (#186)
- `--no-session-cookies` ignores `Set-Cookie` entirely, leaving only the static `-C` cookies — the behaviour to keep when benchmarking a cache or CDN layer. Scenario-level `session: fresh_per_iteration` (default `persistent`) empties the jar at the start of each iteration so every pass is a new visitor. Static `-C` cookies travel in the request header rather than the jar, so they are unaffected by either setting and are still sent on every request. Plain connection mode (`-c`/`-d`) keeps its existing jar behaviour. (#186)

- **Scenario correlation**: scenario steps take an `extract` block that binds response values to variables (`json` JSONPath, `regex` capture group, or response `header`), and later steps reference them as `${var}` in the path, header names/values, and body — including inside nested JSON bodies. Variables are scoped per virtual user and cleared at the start of every iteration, so authenticated flows (login → token → API call) are now testable without cross-user leakage. (#185)
- Scenario-level `on_extract_failure` (`abort_iteration` | `continue`) and `on_template_error` (`abort_iteration` | `keep_literal`) control what happens when a rule yields nothing or a `${var}` is unbound. Failures surface as `extract_failures` / `template_errors` in JSON output, as `Extract Failures` / `Template Errors` in the terminal summary, and as distinct `ExtractFailure: ...` / `TemplateError: ...` keys in the error distribution. (#185)
- New `pywrkr.templating` module with the substitution engine, the in-house dotted JSONPath subset, and one compiler per extraction source — exported from the package root (`substitute`, `apply_extractors`, `compile_json_extractor` / `compile_regex_extractor` / `compile_header_extractor`, the data-driven `compile_extractor` dispatcher, `Extractor`, `TemplateError`, `ExtractError`). Regexes and JSONPaths are compiled when the scenario file loads, so typos are reported as validation errors rather than per-request failures. No new runtime dependencies. (#185)

## [1.5.5] - 2026-07-29

### Security

- Bump `aiohttp` to 3.14.1 and `msgpack` to 1.2.1. (#182)
- Bump `pip` to 26.1.2 in `uv.lock` to resolve CVE-2026-8643 (path traversal in entry point names). (#198)

### Changed

- **Docker**: move the base image to `python:3.15-rc-alpine`. (#183)

### Fixed

- **Docker**: install the build toolchain needed for Alpine wheel compilation. (#184)

## [1.5.4] - 2026-06-15

### Added

- JSON output now includes an `rps_timeline` field (`[seconds_from_start, requests_in_interval]` pairs), so `--json` and programmatic consumers can access the throughput timeline. (#173)
- `run_master` accepts an optional `ready: asyncio.Event` that is set once the listener is bound, letting callers connect only after the bind completes. (#179)

### Fixed

- **Throughput timeline (single-node)**: the console printed only a header and HTML x-axis labels were large negative numbers, because the reporters subtracted the monotonic start time from an already-rebased timeline. Bucketing is now relative to the timeline's own origin. (#172)
- **Thresholds**: a unitless `--threshold` latency expression (e.g. `p99<5`) was silently interpreted as seconds; it now emits a warning naming the assumed unit so a misremembered unit no longer produces a falsely green CI gate. (#174)
- **Tests**: removed a connection race in the distributed worker-authentication tests that intermittently failed CI with `ConnectionRefusedError`. (#178, #179)

## [1.5.3] - 2026-06-12

### Fixed

- Update tests to use canonical imports after the CodeQL cleanup, and fix a `ruff` I001 import-sort error in tests.

### Changed

- Switch the README version badge to GitHub Releases.

## [1.5.2] - 2026-06-12

### Fixed

- Resolve all 10 remaining CodeQL code-scanning alerts.

## [1.5.1] - 2026-06-12

- Maintenance re-release (version bump only; no functional changes).

## [1.5.0] - 2026-06-12

### Added

- **Distributed auth**: HMAC-SHA256 challenge-response authentication for the master/worker protocol. (#165)

### Changed

- **Breaking for `from pywrkr import *` consumers**: removed 15 underscore-prefixed internal names from `__all__` (`_get_metric_value`, `_compare`, `_html_escape`, `_format_latency_short`, `_build_request_headers`, `_create_ssl_context`, `_step_passed`, `_extract_step_result`, `_write_autofind_json`, `_serialize_config`, `_deserialize_config`, `_serialize_stats`, `_deserialize_stats`, `_send_msg`, `_recv_msg`). They were never intended as public API and remain importable from their source modules (`pywrkr.reporting`, `pywrkr.workers`, `pywrkr.distributed`). (#159)
- Derive `__version__` from `importlib.metadata` to remove the dual-location version. (#149)
- Consolidate stats merging into `config.merge_stats` and apply the step-name cap in distributed mode. (#147)
- Unify the percentile formula via `_nearest_rank_idx`. (#118, #142)
- Split `print_results` into separate console-output and file-I/O halves. (#153)

### Security

- Escape dynamic HTML content in the report generators to prevent XSS. (#162)
- Eliminate shell injection in the ECS task and Jenkinsfile. (#163)
- Add SSRF protection to the Jenkins pipeline `TARGET_URL` parameter. (#161)
- Scope AWS credentials to `withCredentials` blocks in the Jenkinsfile. (#157)
- Warn when credentials are used with SSL verification disabled. (#160)

### Fixed

- Allow a list body in `ScenarioStep`. (#119, #141)
- Surface OTel/Prometheus export errors as a non-zero exit code. (#117, #143)
- Initialize `end_time` before the `try` block in `_finalize_run`.
- Create a fresh `trace_ctx` per scenario step.
- Remove the dead `WorkerStats.results` field.

### Performance

- Batch `rps_timeline` by second instead of per-request in the user and scenario workers. (#146)
- Move the LiveDashboard latency sort off the asyncio event loop. (#152)
- Hoist `ClientTimeout` construction outside the request loop for fixed-count runs. (#144)

### Dependencies

- Pin `aiohttp` to `~=3.14` to block future 4.x breakage. (#145)

### CI

- Enforce Codecov thresholds and add a runtime `__version__` check to the release workflow. (#156)
- Add an `all-checks-pass` gate and SHA-pin mutable action refs. (#155)
- Add a `test-otel` job so the OTel integration tests run in CI. (#151)

### Docs

- Lead the README with an animated demo, install, and a minimal example. (#106)
- Document `--ssl-verify` / `--ca-bundle` in the README options table; add pre-commit install to CONTRIBUTING. (#148)
- Warn about ReDoS risk on `--include` / `--exclude` regex patterns. (#164)

## [1.4.5] - 2026-06-06

### Security

- Bump `aiohttp` to `>=3.14.0` to fix CVE-2025-47279 and CVE-2025-47280. (#105)

## [1.4.4] - 2026-05-29

### Fixed

- Resolve audit-confirmed defects across all modules. (#102)

## [1.4.3] - 2026-05-25

### Security

- Bump `idna` 3.13 → 3.16 (CVE-2026-45409). (#100)

## [1.4.2] - 2026-05-18

### Security

- Upgrade `urllib3` to 2.7.0 to fix CVE-2025-4793 and CVE-2025-4435. (#98)

## [1.4.1] - 2026-05-02

### Fixed
- **HAR import**: precompile include/exclude regexes once and bound URL length passed to the matcher to 8192 characters. A pathological pattern such as `^(a+)+$` against a long URL could previously force `re.search` into catastrophic backtracking and freeze the importer; the cap converts that worst case into a fixed, small constant. Invalid regexes now also surface as a clear `ValueError` at filter setup rather than buried in the per-URL loop. (#96)

## [1.4.0] - 2026-05-02

### Fixed
- **Distributed mode** (`--master` / `--worker`): cap incoming message size at 256 MiB to prevent a peer from announcing a 4 GiB payload and forcing the receiver to allocate before any JSON parser runs. The worker also now applies a 300 s timeout to its initial config-receive call so a stalled or disconnected master no longer leaves the worker blocked indefinitely. (#90)
- **HTML report**: when every recorded latency is the same value (a fast in-process server, a single-request smoke run, or a sub-resolution benchmark), the response-time histogram now renders as a single green bar at the actual value instead of stretching across an arbitrary one-second range with all bars painted red. (#91)
- **CLI validation**: reject `--timeout <= 0`, `--ramp-up < 0`, `--think-time < 0`, `--think-jitter` outside `[0, 1]`, and `--rate-ramp <= 0` with a clean usage error rather than letting nonsense values propagate into the worker. (#92)
- **Scenario files**: `load_scenario` now reads YAML/JSON with `encoding="utf-8"` so non-ASCII step names or paths behave identically across platforms (Windows previously decoded with the platform default codec). (#92)
- **HAR import**: per the HAR spec, `postData.encoding == "base64"` indicates `text` is a base64-encoded payload (the form Chrome uses for non-text uploads). The importer was treating the base64 string itself as the request body, so generated scenarios replayed the base64 text rather than the bytes the browser actually sent. The base64 is now decoded; bodies that decode to non-UTF-8 bytes are dropped with a warning rather than silently sending the wrong payload. (#93)
- **Worker stats**: the request-error branch in `_make_request` appended directly to `stats.step_latencies[step_name]`, bypassing the `_MAX_STEP_NAMES` cap that the success path honours. A long benchmark with many distinct error step names could grow the dict without bound. The error branch now goes through `_record_step_latency`. (#94)

## [1.3.7] - 2026-04-27

### Fixed
- Resolve CodeQL `py/import-and-import-from` alerts in tests by switching to local imports. (#85, #86, #87)

### CI
- Migrate dependency management from pip/pip-compile to uv; replace `requirements-dev.txt` with `uv.lock`. (#88)

## [1.3.6] - 2026-04-17

### Fixed
- Resolve remaining CodeQL code scanning alerts (unused/repeated imports, ineffectual statements)

## [1.3.5] - 2026-04-17

### Fixed

- Resolve CodeQL code-scanning alerts. (#79)

### Security

- Bump `aiohttp` and `pytest` to resolve Dependabot security alerts. (#77)

### CI

- Add CodeQL analysis, update `actions/checkout` to v6, and add a CodeQL badge. (#78)

## [1.3.4] - 2026-04-01

### Fixed

- Replace all `python pywrkr.py` invocations with `pywrkr` in README (48 occurrences)
- Update README usage block to match actual `--help` output (was missing 15+ flags)
- Update README Requirements to `pip install pywrkr` instead of `pip install aiohttp`
- Fix test count in CONTRIBUTING.md from ~300 to ~700
- Update SECURITY.md supported versions to 1.3.x

### Removed

- Remove unused `black` from lint dependencies and `[tool.black]` config section
- Regenerate requirements-dev.txt without black and its transitive dependencies

## [1.3.3] - 2026-03-31

### Changed

- Add GOVERNANCE.md with BDFL governance model and path to maintainership
- Add response time expectations (7-day SLA) to CONTRIBUTING.md
- Update PyPI development status classifier from Beta to Production/Stable
- Add README badges (CI, PyPI, Python versions, license, coverage)
- Add Contributing section to README linking to CONTRIBUTING.md and CODE_OF_CONDUCT.md
- Add CHANGELOG.md covering all releases from v0.9.2 to present
- Add dependabot.yml for automated pip and GitHub Actions updates
- Add CODEOWNERS for automatic review assignment
- Add FUNDING.yml for GitHub Sponsors
- Fix PR template linter reference from flake8 to ruff
- Fix GitHub ruleset status check name mismatch
- Fix CONTRIBUTING.md Questions link to point to GitHub Discussions

## [1.3.2] - 2026-03-31

### Security

- Bump Pygments 2.19.2 to 2.20.0 to fix ReDoS vulnerability (CVE-2026-4539)

## [1.3.1] - 2026-03-30

### Fixed

- Bump requests 2.32.5 to 2.33.0
- Use total elapsed time in rate limiter test to avoid CI flakiness

### Changed

- Increase test coverage from 92% to 95%

## [1.3.0] - 2026-03-20

### Added

- Multi-region distributed load testing on AWS ECS/Fargate infrastructure
- CLAUDE.md with repo rules and PR workflow directives
- Sanitize-pr-description workflow
- Coverage tests for main.py validation helpers

### Fixed

- Suppress aiohttp DeprecationWarning on Python 3.12+
- Make rate limiter tests resilient to CI timing jitter
- Relax flaky rate limiter test threshold for macOS CI
- Remove corrupted `.github` tree entry
- Regenerate stale HAR import example files
- Pass CODECOV_TOKEN secret to codecov-action v5
- Upgrade test-results-action from v1 to v5
- Add explicit permissions to sanitize-pr-description workflow

### Changed

- Comprehensive CI pipeline enhancements (lint, pre-commit, test matrix, typecheck, security)

## [1.2.3] - 2026-03-16

### Fixed

- Fix spurious `ClientConnectionError` in request-count mode — shared `TCPConnector` was being closed prematurely by setting `connector_owner=False`
- Add input validation for `--connections`, `--threads`, `--duration`, `--num-requests` CLI parameters
- Fix unhandled tracebacks for `--scenario` file errors with clean argparse messages

### Added

- `base_url` support in scenario files — `--scenario` no longer requires a positional URL argument

## [1.2.2] - 2026-03-16

### Fixed

- Allow `--scenario` without positional url argument

## [1.2.1] - 2026-03-16

### Fixed

- Resolve `RuntimeWarning` when running `python -m pywrkr` by adding `__main__.py` entry point
- Use `None` sentinel for file params to support stdout patching in tests

### Changed

- Refactor traffic_profiles.py — improve validation and parsing
- Refactor workers.py — improve quality, safety, and observability
- Refactor reporting.py — extract chart color constants, add TextIO type hints, consolidate export metrics, extract Gatling HTML report to `string.Template`

## [1.2.0] - 2026-03-12

### Fixed

- Shared connection pool — all worker groups now share a single `TCPConnector`
- Parameter validation — reject invalid CLI arguments with clear error messages
- Rate limiter lock contention — remove unnecessary `asyncio.Lock`
- Error handling and resource cleanup with `try/finally` for `connector.close()`

### Changed

- Memory-bounded sampling — replace unbounded lists with `ReservoirSampler` (Algorithm R)
- Simplify complex functions — extract shared runner lifecycle helpers
- Type safety — replace bare `dict` parameters with typed `ActiveUsers` and `RequestCounter`
- Deduplicate workers — extract `_build_request_headers` and `_merge_all_stats`

### Added

- 81 new tests covering reporting, multi-URL, distributed, and worker utilities
- Field-completeness guard for distributed config serialization
- `ruff format` check in CI workflow

## [1.1.1] - 2026-03-11

### Changed

- Replace all `print()` with structured logging via Python `logging` module
- Narrow `except Exception` to specific exceptions
- Extract helper functions to reduce duplication
- Add docstrings to all major async functions

### Added

- `SSLConfig` dataclass with env var support (`PYWRKR_SSL_VERIFY`, `PYWRKR_CA_BUNDLE`)
- `--ssl-verify` and `--ca-bundle` CLI flags
- `mypy`, `black`, `ruff` configurations in `pyproject.toml`
- 42 new tests covering SSL config, helpers, timeouts, cancellation, and edge cases

## [1.1.0] - 2026-03-11

### Added

- Distributed load testing infrastructure on AWS ECS Fargate with Jenkins orchestration
- Terraform modules — VPC networking, IAM roles, ECR registry, ECS cluster, CloudWatch logging
- Jenkins 10-stage declarative pipeline with parameterized builds
- AWS Cloud Map service discovery for worker-master communication
- Interactive HTML report generator (`generate_report.py`) with Chart.js visualizations
- Comprehensive deployment documentation with architecture diagrams

### Fixed

- Distributed mode correctly passes `html_report` config to the report builder
- Test suite version check no longer hardcodes a specific version string

## [1.0.5] - 2026-03-11

### Added

- HAR / browser-recording import (`pywrkr har-import`) — convert HAR files to pywrkr scenarios or URL lists
- Domain filtering, regex include/exclude patterns, think time derivation
- Two output formats: `scenario` (JSON) and `url-file`
- 41 new tests for HAR parsing, filtering, conversion, and CLI
- Sample HAR file and generated outputs in `examples/`

## [1.0.4] - 2026-03-11

### Fixed

- Fix PyPI publish failure — `pyproject.toml` version now matches release tag

### Changed

- Remove deprecated `@unittest_run_loop` decorator (aiohttp 3.8+)
- Upgrade GitHub Actions to Node.js 24-compatible versions
- Split `_build_parser` into 6 focused helper functions
- PEP 8 import ordering across all modules

## [1.0.3] - 2026-03-11

### Changed

- Decompose 250+ line `main()` into focused functions
- Reduce import complexity — removed 93 lines of re-exports
- Add docstrings, PEP 604 type annotations, extract 12 default constants
- Add community standards: Code of Conduct, contributing guide, security policy, issue/PR templates

### Added

- 11 new tests (parser helpers, default constants validation)
- CodeQL workflow for security analysis
- Examples folder with sample benchmark outputs

## [1.0.2] - 2026-03-11

### Security

- Add explicit `permissions: contents: read` to CI workflow for least-privilege access

## [1.0.1] - 2026-03-11

### Added

- Traffic profiles — realistic traffic shaping (`--traffic-profile`) with 6 built-in shapes: sine, step, sawtooth, square, spike, business-hours
- CSV replay for production traffic curves
- 27 new unit tests for traffic profiles

### Fixed

- Fix f-string backslash syntax for Python 3.10/3.11 compatibility (PEP 701)
- Fix flaky threshold test with floating-point boundary comparison

## [0.9.5] - 2026-03-11

### Added

- Gatling-style interactive HTML reports (`--html-report`)
- Response time distribution histogram, percentile curve, throughput timeline, status code breakdown
- Dark theme, responsive layout, offline-capable
- 20 new tests for HTML reports

## [0.9.2] - 2026-03-11

### Added

- Initial public release
- Five benchmarking modes: duration, request-count, user simulation, rate limiting, auto-ramping
- Detailed latency statistics with percentiles (p50-p99.99) and histogram
- Latency breakdown: DNS, TCP connect, TLS, TTFB, transfer
- SLO-aware thresholds with CI-friendly exit codes
- Rate limiting and rate ramping
- Scripted scenarios (YAML/JSON)
- Live TUI dashboard (optional, via Rich)
- Multi-URL testing from file
- Distributed master/worker mode
- Observability export: OpenTelemetry and Prometheus
- Output formats: terminal, JSON, CSV, HTML

[1.3.4]: https://github.com/kurok/pywrkr/compare/v1.3.3...v1.3.4
[1.3.3]: https://github.com/kurok/pywrkr/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/kurok/pywrkr/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/kurok/pywrkr/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/kurok/pywrkr/compare/v1.2.3...v1.3.0
[1.2.3]: https://github.com/kurok/pywrkr/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/kurok/pywrkr/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/kurok/pywrkr/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/kurok/pywrkr/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/kurok/pywrkr/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/kurok/pywrkr/compare/v1.0.5...v1.1.0
[1.0.5]: https://github.com/kurok/pywrkr/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/kurok/pywrkr/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/kurok/pywrkr/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/kurok/pywrkr/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/kurok/pywrkr/compare/v0.9.5...v1.0.1
[0.9.5]: https://github.com/kurok/pywrkr/compare/v0.9.2...v0.9.5
[0.9.2]: https://github.com/kurok/pywrkr/releases/tag/v0.9.2
