from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from monitor.cpa.billing import cycle_rows, weekly_distribution
from monitor.cpa.participants import owner_index
from monitor.cpa.reporting import account_summary
from monitor.models import CPAAccountCollectionInterval
from monitor.particle_trajectory import particle_trajectory_data
from monitor.replay import rebuild_account
from .test_cpa_participants import setup, event, observation

pytestmark = pytest.mark.django_db


def test_excluded_observation_does_not_expand_week_to_account_history(setup):
    config, _, account, a, b, keys, start = setup
    old_start = start - timedelta(days=2)
    account.created_at = old_start
    account.save(update_fields=["created_at"])
    event(account, keys[0], old_start + timedelta(minutes=10))
    old = observation(account, old_start, old_start + timedelta(hours=1), 20, 10)
    old.attribution_started_at = old_start
    old.save()
    event(account, keys[0], start + timedelta(minutes=10))
    latest = observation(account, start, start + timedelta(hours=1), 29, 10)
    latest.excluded_at = timezone.now()
    latest.exclusion_source = "automatic"
    latest.save()
    result = account_summary(account, config, latest.observed_at, owner_index())
    assert result[0].pk == latest.pk
    assert result[1] == start
    assert result[3]["request_count"] == 1
    assert result[3]["usage_usd"] == 10
    future = observation(account, start + timedelta(days=1), start + timedelta(days=2), 0, 0)
    future.attribution_started_at = future.observed_at
    future.save()
    assert account_summary(account, config, latest.observed_at, owner_index())[0].pk == latest.pk


@pytest.mark.parametrize("model", ["constant_average", "time_varying"])
def test_sustained_cpa_quota_credit_rebases_without_resetting_week(setup, model):
    config, _, account, a, b, keys, start = setup
    config.weekly_quota_model = model
    config.save()
    CPAAccountCollectionInterval.objects.create(account=account, session_key="credit", connected_at=start)
    observation(account, start, start, 40, 0)
    event(account, keys[0], start + timedelta(minutes=5))
    observation(account, start, start + timedelta(minutes=10), 50, 10)
    correction = observation(account, start, start + timedelta(minutes=20), 28, 10)
    observation(account, start, start + timedelta(minutes=30), 28, 10)
    event(account, keys[0], start + timedelta(minutes=35))
    latest = observation(account, start, start + timedelta(minutes=40), 29, 20)
    rebuild_account(account.fact_key, config)
    latest.refresh_from_db()
    assert latest.excluded_at is None
    assert latest.attribution_started_at == correction.observed_at
    assert latest.raw_window["replay_segment_reason"] == "provider_quota_adjustment"
    assert latest.selected_total_cost == 10
    assert latest.interval_used_percent == 1
    week = weekly_distribution([account], {a.id: a, b.id: b}, config, latest.observed_at, owner_index())[0]
    assert week["started_at"] == start.isoformat()
    assert week["usage_usd"] == 20
    assert week["capacity_usd"] > 0
    assert week["upstream_remaining_percent"] == 71
    assert len(cycle_rows(account, config, latest.observed_at)) == 1
    if model == "time_varying":
        assert week["capacity_usd"] == particle_trajectory_data(config, account)["latest"]["capacity_usd"]
    expected = latest.effective_usd_per_percent
    rebuild_account(account.fact_key, config)
    latest.refresh_from_db()
    assert latest.effective_usd_per_percent == expected
    a.refresh_from_db()
    assert a.latest_balance_usd == Decimal("123")


def test_transient_cpa_quota_drop_still_excluded(setup):
    config, _, account, a, b, keys, start = setup
    CPAAccountCollectionInterval.objects.create(account=account, session_key="transient", connected_at=start)
    observation(account, start, start, 40, 0)
    lows = [observation(account, start, start + timedelta(minutes=5 * i), 28, 0) for i in range(1, 4)]
    latest = observation(account, start, start + timedelta(minutes=20), 40, 0)
    rebuild_account(account.fact_key, config)
    for row in lows:
        row.refresh_from_db()
        assert row.exclusion_source == "automatic"
    latest.refresh_from_db()
    assert latest.attribution_started_at == start


def test_v10_exclusions_recover_on_upgrade_without_changing_facts(setup):
    from io import StringIO
    from django.core.management import call_command
    from monitor.models import Observation, CPAUsageEvent
    from monitor.replay import rebuild_observation_suffix

    config, _, account, a, b, keys, start = setup
    config.weekly_quota_model = "time_varying"
    config.save()
    CPAAccountCollectionInterval.objects.create(account=account, session_key="upgrade", connected_at=start)
    observation(account, start, start, 50, 0)
    for i in range(1, 4):
        low = observation(account, start, start + timedelta(minutes=10 * i), 28, 0)
        low.excluded_at = timezone.now()
        low.exclusion_source = "automatic"
        low.raw_window["rate_method"] = "particle_filter_v10"
        low.save()
    fields = ("id", "observed_at", "raw_selected_total_cost", "upstream_used_percent", "upstream_resets_at")
    facts = list(Observation.objects.order_by("id").values(*fields))
    events = list(CPAUsageEvent.objects.values())
    call_command("replayobservations", stdout=StringIO())
    low.refresh_from_db()
    assert low.excluded_at is None
    assert low.raw_window["replay_segment_reason"] == "provider_quota_adjustment"
    assert list(Observation.objects.order_by("id").values(*fields)) == facts
    assert list(CPAUsageEvent.objects.values()) == events
    baseline = low.attribution_started_at
    # Usage eventually reaching the pre-credit percentage must not cancel
    # an already confirmed baseline during a later full replay.
    event(account, keys[0], start + timedelta(minutes=50))
    latest = observation(account, start, start + timedelta(hours=1), 51, 10)
    rebuild_observation_suffix(latest, config)
    latest.refresh_from_db()
    expected = latest.effective_usd_per_percent
    rebuild_account(account.fact_key, config)
    latest.refresh_from_db()
    assert latest.attribution_started_at == baseline
    assert latest.effective_usd_per_percent == expected


def test_repeated_adjustments_wait_for_stable_suffix(setup):
    config, _, account, a, b, keys, start = setup
    CPAAccountCollectionInterval.objects.create(account=account, session_key="repeated", connected_at=start)
    observation(account, start, start, 50, 0)
    unstable = observation(account, start, start + timedelta(minutes=10), 35, 0)
    baseline = observation(account, start, start + timedelta(minutes=20), 28, 0)
    observation(account, start, start + timedelta(minutes=30), 28, 0)
    latest = observation(account, start, start + timedelta(minutes=40), 28, 0)
    rebuild_account(account.fact_key, config)
    unstable.refresh_from_db()
    latest.refresh_from_db()
    assert unstable.exclusion_source == "automatic"
    assert latest.excluded_at is None
    assert latest.attribution_started_at == baseline.observed_at


def test_quota_drop_does_not_swallow_reconnection_baseline(setup):
    config, _, account, a, b, keys, start = setup
    CPAAccountCollectionInterval.objects.create(
        account=account, session_key="before-gap", connected_at=start,
        disconnected_at=start + timedelta(minutes=25), end_reliable=True,
    )
    connected = start + timedelta(minutes=30)
    CPAAccountCollectionInterval.objects.create(
        account=account, session_key="after-gap", connected_at=connected,
    )
    observation(account, start, start, 50, 0)
    observation(account, start, start + timedelta(minutes=10), 28, 0)
    observation(account, start, start + timedelta(minutes=20), 28, 0)
    latest = observation(account, start, connected, 28, 0)
    rebuild_account(account.fact_key, config)
    latest.refresh_from_db()
    assert latest.excluded_at is None
    assert latest.raw_window["replay_segment_reason"] == "provider_collection_baseline"
    assert latest.attribution_started_at == connected
