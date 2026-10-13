"""Read-side work budgets and exact pricing/timezone/authorization regressions."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from monitor.cpa.request_summary import request_summary
from monitor.cpa.usage import cpa_event_cost
from monitor.cpa.usage_rollup import UsageWindow, request_summaries
from monitor.models import CPAUsageEvent
from monitor.reporting import usage
from monitor.reporting.capacity import capacity_series, capacity_summary
from monitor.reporting.costs import FastCorrectionBreakdownPresenter
from monitor.views.account_status import _request_status_stats
from monitor.views import account_status
from monitor.views.cpa_participants import CPARequestsView

from . import test_cpa_participants as fixtures
from .test_cpa_participants import event, member_client, observation

pytestmark = pytest.mark.django_db
setup = fixtures.setup


def gpt(
    event,
    *,
    nanos=123_456_789,
    state="complete",
    cost_state="priced",
    completeness="complete",
):
    event.source = "gpt_load"
    event.source_cost_nano_usd = nanos
    event.usage_state = state
    event.cost_state = cost_state
    event.pricing_completeness = completeness
    event.save()
    return event


@pytest.mark.parametrize("count", [10, 1000, 60_000])
def test_large_gpt_pages_have_constant_queries_and_no_python_pricing(setup, count):
    config, admin, account, _, _, keys, start = setup
    CPAUsageEvent.objects.bulk_create(
        [
            CPAUsageEvent(
                account=account,
                event_fingerprint=f"page-{i}",
                occurred_at=start - timedelta(seconds=i * 10),
                api_key_hash=keys[i % 2].key_hash,
                api_key_hint="test",
                source="gpt_load",
                usage_state="complete",
                cost_state="priced",
                pricing_completeness="complete",
                source_cost_nano_usd=1_234_567,
                input_tokens=100,
                total_tokens=100,
                latency_ms=100,
            )
            for i in range(count)
        ],
        batch_size=500,
    )
    events = CPAUsageEvent.objects.filter(account=account)
    with (
        patch("monitor.cpa.usage_rollup.cpa_event_cost", wraps=cpa_event_cost) as price,
        CaptureQueriesContext(connection) as queries,
    ):
        began = perf_counter()
        result = request_summary(events, config)
        elapsed = perf_counter() - began
    assert len(queries) == 2
    assert price.call_count == 0
    assert result["request_count"] == count
    assert result["usage_usd"] == float(Decimal(1_234_567) * count / 10**9)
    with CaptureQueriesContext(connection) as queries:
        began = perf_counter()
        _, stats = _request_status_stats(
            config, account, start, start - timedelta(days=7)
        )
        status_elapsed = perf_counter() - began
    assert len(queries) == 2
    assert stats["request_count"] == count
    with (
        patch("monitor.reporting.usage.cpa_event_cost", wraps=cpa_event_cost) as price,
        CaptureQueriesContext(connection) as queries,
    ):
        began = perf_counter()
        chart = usage.cpa_api_key_usage_series(
            config=config,
            account=account,
            location=ZoneInfo("UTC"),
            now=start,
            usage_days=30,
            usage_precision="hour",
        )
        chart_elapsed = perf_counter() - began
    assert len(queries) == 3
    assert price.call_count == 0
    assert sum(row["request_count"] for row in chart) == count
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    request = APIRequestFactory().get(
        "/api/cpa/requests",
        {
            "account_id": account.id,
            "include_summary": "true",
            "page_size": 10,
            "started_at": (start - timedelta(days=7)).isoformat(),
            "ended_at": (start + timedelta(seconds=1)).isoformat(),
        },
    )
    force_authenticate(request, admin)
    with CaptureQueriesContext(connection) as queries:
        response = CPARequestsView.as_view()(request)
        response.render()
    assert response.status_code == 200
    assert response.data["data"]["total"] == count
    assert response.data["data"]["summary"]["request_count"] == count
    assert len(queries) <= 12
    print(
        f"\n{count} requests: summary {elapsed:.3f}s/2 SQL; account stats {status_elapsed:.3f}s/2 SQL; hour chart {chart_elapsed:.3f}s/3 SQL"
    )


@pytest.mark.parametrize("overflow", [False, True])
def test_rollups_preserve_mixed_pricing_missing_usage_nanos_and_latency(
    setup, overflow
):
    config, _, account, _, _, keys, start = setup
    config.cpa_fast_multiplier = 3
    config.cpa_double_billing_enabled = True
    config.cpa_double_billing_threshold_tokens = 500_000
    config.cpa_double_billing_multiplier = 2
    cpa = event(account, keys[0], start)
    cpa.requested_service_tier = "priority"
    cpa.latency_ms = 100
    cpa.ttft_ms = 20
    cpa.save()
    nanos = 9_000_000_000_000_000_000 if overflow else 1
    gpt(event(account, keys[0], start), nanos=nanos)
    gpt(event(account, keys[0], start), nanos=nanos, completeness="partial")
    gpt(
        event(account, keys[0], start),
        nanos=999_999_999,
        state="missing",
        cost_state="unpriced",
    )
    gpt(event(account, keys[0], start), nanos=None, cost_state="unpriced")
    event(account, keys[0], start, model="unknown")
    events = CPAUsageEvent.objects.filter(account=account)
    expected = sum((cpa_event_cost(item, config)[0] for item in events), Decimal(0))
    result = request_summaries(events, config, {"all": UsageWindow()})["all"]
    assert result["usage_usd"] == float(expected)
    assert result["request_count"] == 6
    assert result["gpt_load_unpriced_request_count"] == 2
    assert result["cpa_unpriced_request_count"] == 1
    assert result["average_latency_ms"] == 100
    assert result["all_average_latency_ms"] == 100 / 6
    assert result["average_ttft_ms"] == 20


@pytest.mark.parametrize(
    "day", [datetime(2026, 3, 8).date(), datetime(2026, 11, 1).date()]
)
def test_multi_window_date_bounds_match_django_on_dst_days(setup, day):
    config, _, account, _, _, keys, _ = setup
    start = datetime.combine(
        day, datetime.min.time(), tzinfo=ZoneInfo("America/New_York")
    ).astimezone(UTC)
    for i in range(-2, 28):
        at = start + timedelta(hours=i)
        item = event(account, keys[0], at)
        if i % 2:
            gpt(item)
    events = CPAUsageEvent.objects.filter(account=account)
    with timezone.override("America/New_York"):
        result = request_summaries(
            events, config, {"day": UsageWindow(local_date=day, count_days=True)}
        )["day"]
        expected = list(events.filter(occurred_at__date=day))
    assert result["request_count"] == len(expected)
    assert result["request_count"] == (23 if day.month == 3 else 25)
    assert result["actual_days"] == 1
    assert result["usage_usd"] == float(
        sum((cpa_event_cost(item, config)[0] for item in expected), Decimal(0))
    )


def test_account_window_boundaries_and_zero_latency_mean(setup):
    config, _, account, _, _, keys, now = setup
    for at in (
        now - timedelta(days=30, seconds=1),
        now - timedelta(days=30),
        now - timedelta(days=7),
        now,
        now + timedelta(seconds=1),
    ):
        item = gpt(event(account, keys[0], at), nanos=10**9)
        item.latency_ms = 100 if at == now else 0
        item.save()
    with timezone.override("UTC"):
        current, stats = _request_status_stats(
            config, account, now, now - timedelta(days=7)
        )
        today_count = CPAUsageEvent.objects.filter(
            account=account,
            occurred_at__range=(now - timedelta(days=30), now),
            occurred_at__date=now.date(),
        ).count()
    assert current["request_count"] == 2
    assert stats["request_count"] == 3
    assert stats["account_cost_usd"] == 3
    assert stats["actual_days_used"] == 3
    assert stats["avg_duration_ms"] == 100 / 3
    assert stats["today"]["request_count"] == today_count


@pytest.mark.parametrize("provider", ["cpa", "gpt_load"])
@pytest.mark.parametrize("failure", [True, False])
def test_account_upstream_failure_and_provider_cycle_boundaries(
    setup, monkeypatch, provider, failure
):
    config, _, account, _, _, keys, start = setup
    now = start + timedelta(hours=1)
    cycle_start = start - timedelta(days=6)
    gpt(event(account, keys[0], start - timedelta(days=5)), nanos=10**9)
    event(account, keys[0], start)
    config.cpa_management_key_encrypted = "configured"
    config.gpt_load_auth_key_encrypted = "configured"
    exception = (
        account_status.CPAError if provider == "cpa" else account_status.GPTLoadError
    )

    class FakeClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def list_codex_accounts(self):
            return []

        def list_source_accounts(self):
            return []

        def query_weekly_window(self, *_args):
            if failure:
                raise exception("offline")
            return SimpleNamespace(
                reset_at=int((cycle_start + timedelta(days=7)).timestamp()),
                window_seconds=604800,
                used_percent=Decimal(10),
                sampled_at=now.isoformat(),
                reset_after_seconds=1000,
            )

    monkeypatch.setattr(
        account_status,
        "CPAClient" if provider == "cpa" else "GPTLoadClient",
        FakeClient,
    )
    monkeypatch.setattr(account_status, "quota_detail", lambda *_args: {})
    rows = {account.id: {"warnings": [], "usage": None}}
    function = (
        account_status._cpa_status_rows
        if provider == "cpa"
        else account_status._gpt_load_status_rows
    )
    with CaptureQueriesContext(connection) as queries:
        assert function(config, [account], rows, now) is None
    assert len(queries) == 2
    row = rows[account.id]
    assert row["stats"]["request_count"] == 2
    assert row["stats"]["account_cost_usd"] == 11
    if failure:
        assert row["usage"] is None
        assert any("offline" in warning for warning in row["warnings"])
    else:
        current = row["usage"]["seven_day"]
        assert current["request_count"] == (1 if provider == "cpa" else 2)
        assert current["account_cost_usd"] == (10 if provider == "cpa" else 11)


@pytest.mark.parametrize(
    "precision,location",
    [
        ("raw", "UTC"),
        ("hour", "Asia/Shanghai"),
        ("day", "America/New_York"),
        ("hour", "America/New_York"),
        ("hour", "Australia/Lord_Howe"),
    ],
)
def test_chart_aggregate_matches_streaming_and_member_scope(
    setup, monkeypatch, precision, location
):
    config, admin, account, alice, _, keys, _ = setup
    start = datetime(2026, 11, 1, 4, tzinfo=UTC)
    for i in range(15):
        item = event(
            account,
            keys[i % 2],
            start + timedelta(minutes=i * 25),
            model="unknown" if i == 4 else "gpt-test",
        )
        item.api_key_hint = f"hint-{i}"
        item.save()
        if i % 3:
            gpt(
                item,
                nanos=i + 1,
                state="missing" if i == 2 else "complete",
                cost_state="unpriced" if i == 5 else "priced",
            )
    # Earliest request, not lowest primary key, supplies the key name.
    earlier = event(account, keys[0], start - timedelta(minutes=10))
    earlier.api_key_hint = "first"
    earlier.save()
    keys[0].bindings.update(started_at=start - timedelta(days=1))
    user, _, _ = member_client(account, alice)
    arguments = dict(
        config=config,
        account=account,
        location=ZoneInfo(location),
        now=start + timedelta(hours=12),
        usage_days=1,
        usage_precision=precision,
    )
    actual = usage.cpa_api_key_usage_series(**arguments, user=admin)
    member = usage.cpa_api_key_usage_series(**arguments, user=user)
    monkeypatch.setattr(
        usage,
        "_chart_usage_rows",
        lambda events, config, *_: usage._stream_chart_rows(events, config),
    )
    expected = usage.cpa_api_key_usage_series(**arguments, user=admin)
    assert len(actual) == len(expected)
    for row, reference in zip(actual, expected):
        assert row.pop("total_usage_usd") == pytest.approx(
            reference.pop("total_usage_usd"), abs=1e-12
        )
        for point, original in zip(row["points"], reference["points"]):
            assert point.pop("usage_usd") == pytest.approx(
                original.pop("usage_usd"), abs=1e-12
            )
        assert row == reference
    assert len(member) == 1
    assert member[0]["api_key_name"] == "API Key ····first"
    assert member[0]["request_count"] == 9


def test_capacity_charts_do_not_defer_fields_or_load_large_json(setup):
    config, _, account, _, _, _, start = setup
    start = start.replace(hour=12, minute=0, second=0, microsecond=0)
    account.provider = "gpt_load"
    for i in range(10):
        item = observation(account, start, start + timedelta(minutes=i), i, i * 10)
        item.attribution_started_at = start
        item.interval_used_percent = i
        item.raw_window = {"unused": "x" * 4096}
        item.save()
    presenter = FastCorrectionBreakdownPresenter(config, account.fact_key)
    with CaptureQueriesContext(connection) as queries:
        summary = capacity_summary(config, account, ZoneInfo("UTC"), start, presenter)
        history = capacity_series(
            config=config,
            account=account,
            location=ZoneInfo("UTC"),
            now=start,
            capacity_days=30,
            capacity_period="day",
            cost_breakdowns=presenter,
        )
    assert summary["cycle"]["estimate_usd"] == 1000
    assert history[0]["sample_count"] == 9
    assert len(queries) == 3
    assert all(
        '"raw_window"' not in q["sql"] and '"model_diagnostics"' not in q["sql"]
        for q in queries
    )
