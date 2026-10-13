"""Request-list totals computed from the already authorized, filtered queryset."""

from .usage_rollup import UsageWindow, request_summaries


def request_summary(events, config):
    result = request_summaries(events, config, {"all": UsageWindow()})["all"]
    result.pop("all_average_latency_ms")
    result.pop("actual_days")
    return result
