"""Exact, authorized usage rollups without loading every GPT-Load request."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

from django.db import DataError, OperationalError, connection
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from .usage import cpa_event_cost

TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)
EVENT_FIELDS = (
    "source",
    "source_cost_nano_usd",
    "usage_state",
    "cost_state",
    "pricing_completeness",
    "model",
    "requested_service_tier",
    "response_service_tier",
    "failed",
    "latency_ms",
    "ttft_ms",
    "occurred_at",
    *TOKEN_FIELDS,
)
GPT_UNPRICED = (
    Q(source="gpt_load")
    & ~Q(usage_state="missing")
    & (
        Q(cost_state="unpriced")
        | (
            Q(cost_state="priced")
            & ~Q(pricing_completeness__in=("complete", "not_applicable"))
        )
    )
)
GPT_COST = Q(source="gpt_load") & ~Q(usage_state="missing")


@dataclass(frozen=True)
class UsageWindow:
    start: datetime | None = None
    end: datetime | None = None
    local_date: date | None = None
    count_days: bool = False

    def condition(self):
        result = Q()
        if self.start is not None:
            result &= Q(occurred_at__gte=self.start)
        if self.end is not None:
            result &= Q(occurred_at__lte=self.end)
        if self.local_date is not None:
            # Indexed UTC bounds instead of repeated SQLite date conversions.
            # Construct both midnights locally so DST days remain 23/25 hours.
            location = timezone.get_current_timezone()
            day_start = datetime.combine(self.local_date, time.min, tzinfo=location)
            day_end = datetime.combine(
                self.local_date + timedelta(days=1), time.min, tzinfo=location
            )
            result &= Q(occurred_at__gte=day_start, occurred_at__lt=day_end)
        return result

    def contains(self, at):
        return (
            (self.start is None or at >= self.start)
            and (self.end is None or at <= self.end)
            and (
                self.local_date is None
                or at.astimezone(timezone.get_current_timezone()).date()
                == self.local_date
            )
        )


def rollup_annotations(condition=None, *, count_days=False):
    condition = condition if condition is not None else Q()
    result = {
        "request_count": Count("id", filter=condition),
        "failed_count": Count("id", filter=condition & Q(failed=True)),
        "gpt_load_unpriced_request_count": Count("id", filter=condition & GPT_UNPRICED),
        "gpt_cost_nano": Sum("source_cost_nano_usd", filter=condition & GPT_COST),
        "latency_sum": Sum("latency_ms", filter=condition),
        "latency_count": Count("id", filter=condition & Q(latency_ms__gt=0)),
        "ttft_sum": Sum("ttft_ms", filter=condition),
        "ttft_count": Count("id", filter=condition & Q(ttft_ms__gt=0)),
        **{field: Sum(field, filter=condition) for field in TOKEN_FIELDS},
    }
    if count_days:
        result["actual_days"] = Count(
            TruncDate("occurred_at", tzinfo=timezone.get_current_timezone()),
            distinct=True,
            filter=condition,
        )
    return result


def _finish(row, cpa_cost=Decimal(0), cpa_unknown=0):
    result = {
        field: int(row.get(field) or 0)
        for field in (
            "request_count",
            "failed_count",
            *TOKEN_FIELDS,
            "gpt_load_unpriced_request_count",
        )
    }
    result["cpa_unpriced_request_count"] = cpa_unknown
    result["unpriced_request_count"] = (
        cpa_unknown + result["gpt_load_unpriced_request_count"]
    )
    result["usage_usd"] = float(
        cpa_cost + Decimal(row.get("gpt_cost_nano") or 0) / Decimal("1000000000")
    )
    for field in ("latency", "ttft"):
        count = row.get(f"{field}_count") or 0
        result[f"average_{field}_ms"] = (
            (row.get(f"{field}_sum") or 0) / count if count else None
        )
    count = result["request_count"]
    result["all_average_latency_ms"] = (
        (row.get("latency_sum") or 0) / count if count else None
    )
    result["actual_days"] = row.get("actual_days", 0)
    return result


def _stream_summaries(events, config, windows):
    """Preserve exact Python integer sums if SQLite's signed SUM overflows."""
    rows = {name: {} for name in windows}
    costs = {name: Decimal(0) for name in windows}
    unknowns = {name: 0 for name in windows}
    days = {name: set() for name in windows}
    for data in events.order_by().values(*EVENT_FIELDS).iterator(chunk_size=2000):
        event = SimpleNamespace(**data)
        cost, unknown = cpa_event_cost(event, config)
        for name, window in windows.items():
            if not window.contains(event.occurred_at):
                continue
            row = rows[name]
            additions = {
                "request_count": 1,
                "failed_count": int(event.failed),
                "latency_sum": event.latency_ms,
                "latency_count": int(event.latency_ms > 0),
                "ttft_sum": event.ttft_ms,
                "ttft_count": int(event.ttft_ms > 0),
                **{field: getattr(event, field) for field in TOKEN_FIELDS},
            }
            for key, amount in additions.items():
                row[key] = row.get(key, 0) + amount
            costs[name] += cost
            if event.source == "gpt_load":
                row["gpt_load_unpriced_request_count"] = row.get(
                    "gpt_load_unpriced_request_count", 0
                ) + int(unknown)
            else:
                unknowns[name] += int(unknown)
            if window.count_days:
                days[name].add(
                    event.occurred_at.astimezone(timezone.get_current_timezone()).date()
                )
    for name in windows:
        rows[name]["actual_days"] = len(days[name])
    return {name: _finish(rows[name], costs[name], unknowns[name]) for name in windows}


def request_summaries(events, config, windows):
    """One SQL aggregate for all windows; CPA prices are evaluated once per event.

    The caller supplies an already authorized queryset. No account/user-wide
    caching or replacement queryset is used here.
    """
    annotations = {}
    field_names = {}
    for name, window in windows.items():
        fields = rollup_annotations(window.condition(), count_days=window.count_days)
        field_names[name] = fields
        annotations.update(
            {f"{name}_{field}": expression for field, expression in fields.items()}
        )
    try:
        totals = events.order_by().aggregate(**annotations)
    except (OperationalError, DataError) as exc:
        if connection.vendor != "sqlite" or "integer overflow" not in str(exc).lower():
            raise
        return _stream_summaries(events, config, windows)
    costs = {name: Decimal(0) for name in windows}
    unknowns = {name: 0 for name in windows}
    for data in (
        events.exclude(source="gpt_load")
        .order_by()
        .values(*EVENT_FIELDS)
        .iterator(chunk_size=2000)
    ):
        event = SimpleNamespace(**data)
        cost, unknown = cpa_event_cost(event, config)
        for name, window in windows.items():
            if window.contains(event.occurred_at):
                costs[name] += cost
                unknowns[name] += int(unknown)
    return {
        name: _finish(
            {field: totals[f"{name}_{field}"] for field in fields},
            costs[name],
            unknowns[name],
        )
        for name, fields in field_names.items()
    }
