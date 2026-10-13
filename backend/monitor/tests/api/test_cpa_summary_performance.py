"""Large dashboard fixture and query/work regression checks."""

from bisect import bisect_left, bisect_right
from datetime import timedelta
from decimal import Decimal
from time import perf_counter
from unittest.mock import patch

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from monitor.cpa.billing import billing_bounds
from monitor.cpa.reporting import pool_summary
from monitor.cpa.quota_discrepancy import current_quota_discrepancy, quota_discrepancies
from monitor.cpa.summary_projection import RequestLedger, SummaryProjection
from monitor.cpa.usage import cpa_event_cost
from monitor.models import CPAAccountCollectionInterval, CPAUsageEvent, Observation

from . import test_cpa_participants as participant_fixtures
from .test_cpa_participants import event, observation
from .test_cpa_quota_discrepancy import gpt_load_discrepancy

pytestmark = pytest.mark.django_db
setup = participant_fixtures.setup


def large_gpt_summary(setup, count=20_000, observations_per_cycle=200):
    config, admin, account, alice, bob, keys, start = setup
    start -= timedelta(days=28)
    now = start + timedelta(days=28, hours=1)
    account.provider = "gpt_load"
    account.cpa_auth_index = None
    account.gpt_load_group_id = 7
    account.gpt_load_credential_id = 11
    account.gpt_load_cutover_at = start
    account.gpt_load_logs_synced_through = now
    account.created_at = start
    account.save()
    account.cpa_contracts.update(effective_at=start)
    for key in keys:
        key.bindings.update(started_at=start)
    account.pool.cpa_billing_anchor = start.date()
    account.pool.cpa_billing_timezone = "UTC"
    account.pool.save()
    config.weekly_quota_model = "time_varying"
    config.save()
    CPAAccountCollectionInterval.objects.create(
        account=account, session_key="summary-performance", connected_at=start
    )
    step = (now - start).total_seconds() / count
    times = [start + timedelta(seconds=step * i) for i in range(count)]
    CPAUsageEvent.objects.bulk_create(
        [
            CPAUsageEvent(
                account=account,
                event_fingerprint=f"summary-{i}",
                occurred_at=times[i],
                api_key_hash=keys[i % 2].key_hash,
                model="gpt-test",
                source="gpt_load",
                source_cost_nano_usd=100_000_000,
                usage_state="complete",
                cost_state="priced",
                pricing_completeness="complete",
                total_tokens=100,
            )
            for i in range(count)
        ],
        batch_size=500,
    )
    observations = []
    for cycle in range(5):
        cycle_start = start + timedelta(days=cycle * 7)
        cycle_end = cycle_start + timedelta(days=7)
        for i in range(observations_per_cycle):
            at = cycle_start + timedelta(
                seconds=(min(now, cycle_end) - cycle_start).total_seconds()
                * i
                / observations_per_cycle
            )
            cost = (
                Decimal(bisect_right(times, at) - bisect_left(times, cycle_start)) / 10
            )
            observations.append(
                Observation(
                    account_id=account.fact_key,
                    observed_at=at,
                    upstream_resets_at=cycle_end,
                    window_seconds=604800,
                    upstream_used_percent=cost / 10,
                    interval_used_percent=cost / 10,
                    attribution_started_at=cycle_start,
                    effective_usd_per_percent=10,
                    raw_selected_total_cost=cost,
                    selected_total_cost=cost,
                    total_standard_cost=cost,
                    total_actual_cost=cost,
                    cost_window_started_at=cycle_start,
                    cost_window_ended_at=at,
                    valid_sample=True,
                    model_diagnostics={"algorithm": "performance-test"},
                    capacity_lower_usd=900,
                    capacity_upper_usd=1100,
                    raw_window={"provider": "gpt_load", "body": {"unused": "x" * 4096}},
                )
            )
    Observation.objects.bulk_create(observations, batch_size=100)
    return config, admin, account, now, count


@pytest.mark.parametrize(
    "count,observations_per_cycle", [(20_000, 200), (60_000, 1000)]
)
def test_large_dashboard_work(setup, monkeypatch, count, observations_per_cycle):
    config, admin, account, now, event_count = large_gpt_summary(
        setup, count, observations_per_cycle
    )
    monkeypatch.setattr("monitor.cpa.reporting.timezone.now", lambda: now)
    from monitor.cpa import quota_discrepancy

    with (
        patch(
            "monitor.cpa.summary_projection.cpa_event_cost", wraps=cpa_event_cost
        ) as priced,
        patch.object(
            quota_discrepancy,
            "_discrepancy_for_group",
            wraps=quota_discrepancy._discrepancy_for_group,
        ) as discrepancies,
        CaptureQueriesContext(connection) as queries,
    ):
        started = perf_counter()
        summary = pool_summary(admin, account, config)
        elapsed = perf_counter() - started
    print(
        f"\nDashboard benchmark: {event_count} events, {observations_per_cycle * 5} observations, {len(queries)} SQL queries, {elapsed:.3f}s, summary pricing calls={priced.call_count}"
    )
    billing_start, billing_end = billing_bounds(
        account.pool.cpa_billing_anchor,
        account.pool.cpa_billing_timezone,
        now,
    )
    expected_billing_count = CPAUsageEvent.objects.filter(
        account=account,
        occurred_at__gte=billing_start,
        occurred_at__lt=min(now, billing_end),
    ).count()
    assert (
        summary["billing_summary"]["request_usage_usd"] == expected_billing_count / 10
    )
    assert priced.call_count == event_count
    assert discrepancies.call_count == 1
    assert len(queries) < 70
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    # Also measure the actual dashboard view, without mock pricing overhead.
    from monitor.views.dashboard import DashboardView

    request = APIRequestFactory().get("/api/dashboard", {"account_id": account.id})
    force_authenticate(request, admin)
    started = perf_counter()
    response = DashboardView.as_view()(request)
    response.render()
    print(
        f"Dashboard API benchmark: {event_count} events, {perf_counter() - started:.3f}s"
    )
    assert response.status_code == 200
    assert response.data["data"]["cpa_summary"] == summary


@pytest.mark.parametrize("include_start", [True, False])
@pytest.mark.parametrize("include_end", [True, False])
def test_ledger_window_boundaries_and_backward_expansion(
    setup, include_start, include_end
):
    config, _, account, _, _, keys, start = setup
    middle = start + timedelta(minutes=10)
    end = start + timedelta(minutes=20)
    events = [event(account, keys[0], at) for at in (start, middle, middle, end)]
    # CPA prices still use current settings; GPT-Load costs retain upstream nanos.
    gpt = events[1]
    gpt.source = "gpt_load"
    gpt.source_cost_nano_usd = 123_456_789
    gpt.usage_state = "complete"
    gpt.cost_state = "priced"
    gpt.pricing_completeness = "partial"
    gpt.save()
    missing = events[2]
    missing.source = "gpt_load"
    missing.source_cost_nano_usd = 999_000_000_000
    missing.usage_state = "missing"
    missing.save()
    options = dict(include_start=include_start, include_end=include_end)
    expected = [
        e
        for e in events
        if (e.occurred_at >= start if include_start else e.occurred_at > start)
        and (e.occurred_at <= end if include_end else e.occurred_at < end)
    ]
    with patch(
        "monitor.cpa.summary_projection.cpa_event_cost", wraps=cpa_event_cost
    ) as priced:
        ledger = RequestLedger(account, config, end)
        assert ledger.totals(middle, end) == (Decimal("10.123456789"), 1)
        assert ledger.totals(start, end, **options) == (
            sum((cpa_event_cost(e, config)[0] for e in expected), Decimal(0)),
            sum(cpa_event_cost(e, config)[1] for e in expected),
        )
        assert [row[0].pk for row in ledger.between(start, end, **options)] == [
            e.pk for e in expected
        ]
        assert ledger.totals(start, end, gpt_load_only=True) == (
            Decimal("0.123456789"),
            1,
        )
        assert ledger.totals(end, start) == (0, 0)
        assert priced.call_count == 4


def test_current_discrepancy_matches_history_without_computing_closed_cycles(setup):
    config, _, account, _, _, _, start, _, latest = gpt_load_discrepancy(setup)
    old = Observation.objects.get(
        account_id=account.fact_key, observed_at__lt=account.gpt_load_cutover_at
    )
    old.raw_window = {"provider": "gpt_load"}
    old.upstream_resets_at -= timedelta(days=7)
    old.save()
    account.gpt_load_cutover_at = start - timedelta(days=7)
    account.save(update_fields=["gpt_load_cutover_at"])
    history = quota_discrepancies(account, config, latest.observed_at)
    assert len(history) == 2
    from monitor.cpa import quota_discrepancy

    with patch.object(
        quota_discrepancy,
        "_discrepancy_for_group",
        wraps=quota_discrepancy._discrepancy_for_group,
    ) as compute:
        assert (
            current_quota_discrepancy(account, config, latest.observed_at)
            == history[-1]
        )
        assert compute.call_count == 1
    assert current_quota_discrepancy(account, config, latest.upstream_resets_at) is None


def test_projection_does_not_cache_prices_or_events_between_refreshes(
    setup, monkeypatch
):
    config, admin, account, _, _, keys, start = setup
    now = start + timedelta(hours=1)
    monkeypatch.setattr("monitor.cpa.reporting.timezone.now", lambda: now)
    event(account, keys[0], start + timedelta(minutes=10))
    assert pool_summary(admin, account, config)["members"][0]["request_usage_usd"] == 10
    config.cpa_model_pricing["gpt-test"]["input"] = "20"
    config.save()
    event(account, keys[0], start + timedelta(minutes=20))
    assert pool_summary(admin, account, config)["members"][0]["request_usage_usd"] == 40


def test_projection_cycle_copies_and_lightweight_observations(setup):
    config, _, account, _, _, _, _, _, latest = gpt_load_discrepancy(setup)
    Observation.objects.filter(pk=latest.pk).update(
        raw_window={
            "provider": "gpt_load",
            "replay_segment_reason": "provider_collection_baseline",
            "body": {"large_unused_response": "x" * 10_000},
        }
    )
    projection = SummaryProjection(config, latest.observed_at)
    with CaptureQueriesContext(connection) as queries:
        rows = projection.observations(account)
        assert projection.observations(account) is rows
    assert len(queries) == 1
    assert rows[-1].raw_window == {
        "provider": "gpt_load",
        "replay_segment_reason": "provider_collection_baseline",
    }
    cycles = projection.cycles(account)
    cycles[0]["end"] += timedelta(days=100)
    cycles.append({"prediction": 100})
    clean = projection.cycles(account)
    assert len(clean) == 1
    assert clean[0]["end"] == latest.upstream_resets_at


def test_dashboard_exposes_timing_and_logs_slow_calculation(setup, monkeypatch, caplog):
    from monitor.views.dashboard import DashboardView

    _, admin, _, _, _, _, _ = setup
    ticks = iter([10, 13])
    monkeypatch.setattr("monitor.views.dashboard.perf_counter", lambda: next(ticks))
    request = APIRequestFactory().get("/api/dashboard")
    force_authenticate(request, admin)
    response = DashboardView.as_view()(request)
    assert response.status_code == 200
    assert response["Server-Timing"] == "dashboard;dur=3000.0"
    assert "Slow dashboard calculation: 3.000s" in caplog.text


def test_current_discrepancy_does_not_price_prebaseline_events(setup):
    config, _, account, _, _, keys, start, _, latest = gpt_load_discrepancy(setup)
    event(account, keys[0], start - timedelta(days=365))
    with patch(
        "monitor.cpa.summary_projection.cpa_event_cost", wraps=cpa_event_cost
    ) as priced:
        current_quota_discrepancy(account, config, latest.observed_at)
    assert priced.call_count == 1


def test_current_discrepancy_preserves_adjacent_reset_drift_and_baseline(setup):
    config, _, account, _, _, _, start, baseline, latest = gpt_load_discrepancy(setup)
    for i in (1, 2):
        tail = observation(
            account, start, latest.observed_at + timedelta(minutes=i), 30 + i, 10
        )
        tail.upstream_resets_at = latest.upstream_resets_at + timedelta(minutes=9 * i)
        tail.raw_window = {"provider": "gpt_load"}
        tail.save()
    account.gpt_load_logs_synced_through = tail.observed_at
    account.save(update_fields=["gpt_load_logs_synced_through"])
    history = quota_discrepancies(account, config, tail.observed_at)
    assert len(history) == 1
    current = current_quota_discrepancy(account, config, tail.observed_at)
    assert current == history[0]
    assert current.baseline_observation_id == baseline.pk
