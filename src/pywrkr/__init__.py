"""pywrkr - A Python HTTP benchmarking tool inspired by wrk and Apache ab."""

from pywrkr.main import (  # noqa: F401
    # Data structures
    RequestResult,
    LatencyBreakdown,
    WorkerStats,
    BenchmarkConfig,
    Threshold,
    AutofindConfig,
    StepResult,
    ScenarioStep,
    Scenario,
    UrlEntry,
    MultiUrlResult,
    # Helpers
    RateLimiter,
    LiveDashboard,
    load_scenario,
    make_url,
    create_trace_config,
    aggregate_breakdowns,
    format_bytes,
    format_duration,
    print_latency_histogram,
    compute_percentiles,
    parse_threshold,
    evaluate_thresholds,
    print_threshold_results,
    print_percentiles,
    print_rps_timeline,
    build_results_dict,
    write_csv_output,
    write_json_output,
    generate_html_report,
    export_to_otel,
    export_to_prometheus,
    print_results,
    # Runners
    worker,
    user_worker,
    scenario_worker,
    show_progress,
    run_benchmark,
    run_user_simulation,
    run_autofind,
    print_autofind_summary,
    # Multi-URL
    load_url_file,
    print_multi_url_summary,
    build_multi_url_json,
    run_multi_url,
    # Distributed
    merge_worker_stats,
    run_master,
    run_worker_node,
    # CLI
    parse_header,
    # Constants
    RICH_AVAILABLE,
    OTEL_AVAILABLE,
    # Private but used by tests
    _compare,
    _deserialize_config,
    _deserialize_stats,
    _extract_step_result,
    _format_latency_short,
    _get_metric_value,
    _recv_msg,
    _send_msg,
    _serialize_config,
    _serialize_stats,
    _step_passed,
    _write_autofind_json,
)

# Re-export main function under a different name to avoid shadowing pywrkr.main module
from pywrkr.main import main as cli_main  # noqa: F401

__version__ = "0.9.1"
