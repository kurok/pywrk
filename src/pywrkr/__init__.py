"""pywrkr - a pure-Python HTTP benchmarking tool and library.

The supported library API is small and documented::

    import pywrkr

    result = pywrkr.run("https://api.example.com/health", connections=50, duration=30)
    assert result.percentiles.p95 < 0.3

:data:`__all__` is that public surface, and it is what the project's
semantic-versioning promise covers: breaking changes to these names require a
major release. Everything else in this package is an implementation detail and
may move without notice.
"""

__all__ = [
    # -- the public library API ------------------------------------------
    "run",
    "arun",
    "Config",
    "Result",
    "Latency",
    "Percentiles",
    "ThresholdVerdict",
    "LiveStats",
    "load_scenario",
    "__version__",
    # -- established types, also covered by the versioning promise -------
    # pywrkr.config
    "RequestResult",
    "LatencyBreakdown",
    "WorkerStats",
    "BenchmarkConfig",
    "Threshold",
    "AutofindConfig",
    "StepResult",
    "ScenarioStep",
    "Scenario",
    "load_scenario",
    "parse_data_spec",
    "parse_extract_spec",
    "validate_scenario_templates",
    "SESSION_CHOICES",
    "SSLConfig",
    # pywrkr.assertions
    "ANY_VALUE",
    "HeaderAssertion",
    "JsonAssertion",
    "StepAssertions",
    "evaluate_assertions",
    "parse_duration",
    "parse_step_assertions",
    # pywrkr.backends
    "AiohttpBackend",
    "Backend",
    "BackendResponse",
    "BackendSession",
    "BackendUnavailableError",
    "HttpxBackend",
    "create_backend",
    "http2_available",
    # pywrkr.compare
    "COMPARE_FORMATS",
    "SCHEMA_VERSION",
    "ComparisonReport",
    "FailOn",
    "MetricDelta",
    "ResultsError",
    "Verdict",
    "average_results",
    "compare_results",
    "load_baseline",
    "load_results",
    "metric_value",
    "parse_fail_on",
    "render_report",
    # pywrkr.feeders
    "FEEDER_STRATEGIES",
    "DataRuntime",
    "Feeder",
    "FeederCursor",
    "load_feeder",
    "shard_rows",
    "validate_unique_capacity",
    # pywrkr.templating
    "Extractor",
    "ExtractError",
    "TemplateError",
    "TemplateFunctions",
    "apply_extractors",
    "compile_extractor",
    "compile_header_extractor",
    "compile_json_extractor",
    "compile_regex_extractor",
    "substitute",
    "substitute_structure",
    "validate_function_call",
    # pywrkr.traffic_profiles
    "TrafficProfile",
    "SineProfile",
    "StepProfile",
    "SawtoothProfile",
    "SquareProfile",
    "SpikeProfile",
    "BusinessHoursProfile",
    "CsvProfile",
    "parse_traffic_profile",
    "RateLimiter",
    # pywrkr.reporting
    "RICH_AVAILABLE",
    "OTEL_AVAILABLE",
    "describe_session_mode",
    "format_bytes",
    "format_duration",
    "print_latency_histogram",
    "compute_percentiles",
    "parse_threshold",
    "evaluate_thresholds",
    "print_threshold_results",
    "print_percentiles",
    "print_rps_timeline",
    "build_results_dict",
    "build_step_stats",
    "print_step_table",
    "write_csv_output",
    "write_json_output",
    "generate_html_report",
    "generate_gatling_html_report",
    "write_html_report",
    "export_to_otel",
    "export_to_prometheus",
    "print_results",
    "print_autofind_summary",
    "print_multi_url_summary",
    "build_multi_url_json",
    "aggregate_breakdowns",
    # pywrkr.workers
    "merge_stats",
    "run_benchmark",
    "run_user_simulation",
    "run_autofind",
    # pywrkr.distributed
    "merge_worker_stats",
    "run_master",
    "run_worker_node",
    # pywrkr.multi_url
    "UrlEntry",
    "MultiUrlResult",
    "load_url_file",
    "run_multi_url",
    # pywrkr.har_import
    "HarEntry",
    "HarImportConfig",
    "parse_har",
    "filter_entries",
    "har_to_scenario",
    "har_to_url_file",
    "convert_har",
]

from pywrkr.api import (
    Config,
    Latency,
    LiveStats,
    Percentiles,
    Result,
    ThresholdVerdict,
    arun,
    run,
)
from pywrkr.assertions import (
    ANY_VALUE,
    HeaderAssertion,
    JsonAssertion,
    StepAssertions,
    evaluate_assertions,
    parse_duration,
    parse_step_assertions,
)
from pywrkr.backends import (
    AiohttpBackend,
    Backend,
    BackendResponse,
    BackendSession,
    BackendUnavailableError,
    HttpxBackend,
    create_backend,
    http2_available,
)
from pywrkr.compare import (
    COMPARE_FORMATS,
    SCHEMA_VERSION,
    ComparisonReport,
    FailOn,
    MetricDelta,
    ResultsError,
    Verdict,
    average_results,
    compare_results,
    load_baseline,
    load_results,
    metric_value,
    parse_fail_on,
    render_report,
)
from pywrkr.config import (
    SESSION_CHOICES,
    AutofindConfig,
    BenchmarkConfig,
    LatencyBreakdown,
    RequestResult,
    Scenario,
    ScenarioStep,
    SSLConfig,
    StepResult,
    Threshold,
    WorkerStats,
    load_scenario,
    merge_stats,
    parse_data_spec,
    parse_extract_spec,
    validate_scenario_templates,
)
from pywrkr.distributed import (
    merge_worker_stats,
    run_master,
    run_worker_node,
)
from pywrkr.feeders import (
    FEEDER_STRATEGIES,
    DataRuntime,
    Feeder,
    FeederCursor,
    load_feeder,
    shard_rows,
    validate_unique_capacity,
)
from pywrkr.har_import import (
    HarEntry,
    HarImportConfig,
    convert_har,
    filter_entries,
    har_to_scenario,
    har_to_url_file,
    parse_har,
)
from pywrkr.multi_url import (
    MultiUrlResult,
    UrlEntry,
    load_url_file,
    run_multi_url,
)
from pywrkr.reporting import (
    OTEL_AVAILABLE,
    RICH_AVAILABLE,
    aggregate_breakdowns,
    build_multi_url_json,
    build_results_dict,
    build_step_stats,
    compute_percentiles,
    describe_session_mode,
    evaluate_thresholds,
    export_to_otel,
    export_to_prometheus,
    format_bytes,
    format_duration,
    generate_gatling_html_report,
    generate_html_report,
    parse_threshold,
    print_autofind_summary,
    print_latency_histogram,
    print_multi_url_summary,
    print_percentiles,
    print_results,
    print_rps_timeline,
    print_step_table,
    print_threshold_results,
    write_csv_output,
    write_html_report,
    write_json_output,
)
from pywrkr.templating import (
    ExtractError,
    Extractor,
    TemplateError,
    TemplateFunctions,
    apply_extractors,
    compile_extractor,
    compile_header_extractor,
    compile_json_extractor,
    compile_regex_extractor,
    substitute,
    substitute_structure,
    validate_function_call,
)
from pywrkr.traffic_profiles import (
    BusinessHoursProfile,
    CsvProfile,
    RateLimiter,
    SawtoothProfile,
    SineProfile,
    SpikeProfile,
    SquareProfile,
    StepProfile,
    TrafficProfile,
    parse_traffic_profile,
)
from pywrkr.workers import (
    run_autofind,
    run_benchmark,
    run_user_simulation,
)

# Worker coroutines leaked into the package namespace before the library API
# existed. They stay importable for one minor release so nothing breaks
# overnight, but reaching for them now says so.
_DEPRECATED_ATTRS = {
    "worker": "pywrkr.workers",
    "user_worker": "pywrkr.workers",
    "scenario_worker": "pywrkr.workers",
    "show_progress": "pywrkr.workers",
    "create_trace_config": "pywrkr.backends",
    "make_url": "pywrkr.workers",
    "LiveDashboard": "pywrkr.workers",
}


def __getattr__(name: str):
    """Serve deprecated names with a warning (PEP 562)."""
    module_path = _DEPRECATED_ATTRS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    import warnings

    warnings.warn(
        f"pywrkr.{name} is an internal helper and is no longer part of the public "
        f"API; import it from {module_path} if you really need it. "
        f"See pywrkr.__all__ for the supported surface.",
        DeprecationWarning,
        stacklevel=2,
    )
    return getattr(importlib.import_module(module_path), name)


try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("pywrkr")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
