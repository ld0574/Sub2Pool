"""Exact input invalidation, bounded artifacts, and fresh response permissions."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event
from time import perf_counter
from unittest.mock import Mock, patch

from django.core.cache import caches
from django.db import connection
from django.test.utils import CaptureQueriesContext
import numpy as np
import pytest

from monitor.accounting.dynamic_contracts import DynamicModelInput
from monitor.accounting.particle_filter import ParticleFilterConfig
from monitor.accounting import trajectory_cache as cache_module
from monitor.models import ParticipantSnapshot
from monitor.particle_trajectory import (
    particle_trajectory_data,
    run_adaptive_range_filter,
)
from monitor.quota_profiles import PRO_20X_CAPACITY_PROFILE

from . import test_cpa_participants as fixtures
from .test_cpa_participants import observation

setup = fixtures.setup


@pytest.fixture(autouse=True)
def clear_trajectory_cache():
    caches["trajectory"].clear()
    yield
    caches["trajectory"].clear()


@pytest.fixture
def inputs():
    model = DynamicModelInput(
        times_hours=np.array([0.0, 1.0]),
        costs_usd=np.array([[0.0, 0.0], [10.0, 20.0]]),
        displayed_percent=np.array([0.0, 2.0]),
        rights_percent=np.array([50.0, 50.0]),
    )
    args = dict(
        account_id=7,
        algorithm="test-version",
        seed=123,
        config=ParticleFilterConfig(),
        capacity_profile=PRO_20X_CAPACITY_PROFILE,
    )
    return model, args


@pytest.mark.parametrize(
    "field",
    [
        "times_hours",
        "costs_usd",
        "displayed_percent",
        "rights_percent",
        "baseline_display_percent",
        "baseline_exact_zero",
        "dtype",
        "shape",
        "account_id",
        "algorithm",
        "seed",
        "config",
        "capacity_profile",
    ],
)
def test_every_model_input_invalidates_cache(inputs, field):
    model, args = inputs
    compute = Mock(return_value={"result": np.array([1.0])})
    cache_module.cached_replay(model, **args, compute=compute)
    cached = cache_module.cached_replay(model, **args, compute=compute)
    assert compute.call_count == 1
    cached["result"][0] = 999
    assert cache_module.cached_replay(model, **args, compute=compute)["result"][0] == 1
    if field in {"times_hours", "costs_usd", "displayed_percent", "rights_percent"}:
        value = getattr(model, field).copy()
        value.flat[-1] += 0.001
        model = replace(model, **{field: value})
    elif field == "dtype":
        model = replace(model, rights_percent=model.rights_percent.astype("float32"))
    elif field == "shape":
        model = replace(model, costs_usd=model.costs_usd.reshape(1, 4))
    elif field == "baseline_display_percent":
        model = replace(model, baseline_display_percent=1.0)
    elif field == "baseline_exact_zero":
        model = replace(model, baseline_exact_zero=False)
    elif field == "config":
        args[field] = replace(args[field], particles=32)
    elif field == "capacity_profile":
        args[field] = replace(args[field], upper_stages_usd=(7000.0,))
    elif field == "algorithm":
        args[field] += "-next"
    else:
        args[field] += 1
    cache_module.cached_replay(model, **args, compute=compute)
    assert compute.call_count == 2


def test_oversized_corrupt_failed_and_evicted_cache_is_optional(inputs, monkeypatch):
    model, args = inputs
    compute = Mock(return_value={"result": "large"})
    monkeypatch.setattr(cache_module, "MAX_ARTIFACT_BYTES", 1)
    for _ in range(2):
        assert cache_module.cached_replay(model, **args, compute=compute) == {
            "result": "large"
        }
    assert compute.call_count == 2
    monkeypatch.setattr(cache_module, "MAX_ARTIFACT_BYTES", 1024)
    key = cache_module.replay_fingerprint(model, **args)
    caches["trajectory"].set(key, b"corrupt")
    assert cache_module.cached_replay(model, **args, compute=compute) == {
        "result": "large"
    }
    caches["trajectory"].delete(key)
    cache_module.cached_replay(model, **args, compute=compute)
    assert compute.call_count == 4
    with (
        patch.object(
            type(caches["trajectory"]),
            "get",
            side_effect=RuntimeError("cache unavailable"),
        ),
        patch.object(
            type(caches["trajectory"]),
            "set",
            side_effect=RuntimeError("cache unavailable"),
        ),
    ):
        assert cache_module.cached_replay(model, **args, compute=compute) == {
            "result": "large"
        }


def test_concurrent_identical_replays_compute_once(inputs):
    model, args = inputs
    started, release = Event(), Event()

    def calculate(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        return "computed"

    compute = Mock(side_effect=calculate)
    with ThreadPoolExecutor(max_workers=4) as executor:
        first = executor.submit(
            cache_module.cached_replay, model, **args, compute=compute
        )
        assert started.wait(5)
        rest = [
            executor.submit(cache_module.cached_replay, model, **args, compute=compute)
            for _ in range(3)
        ]
        release.set()
        assert [future.result(timeout=5) for future in [first, *rest]] == [
            "computed"
        ] * 4
    assert compute.call_count == 1


def test_cache_is_bounded_and_separate_from_auth(inputs):
    model, args = inputs
    auth = caches["default"]
    auth.set("trajectory-test-auth", "preserved")
    keys = []
    for i in range(12):
        arguments = {**args, "account_id": i}
        cache_module.cached_replay(model, **arguments, compute=Mock(return_value=i))
        keys.append(cache_module.replay_fingerprint(model, **arguments))
    assert sum(caches["trajectory"].get(key) is not None for key in keys) <= 4
    assert auth.get("trajectory-test-auth") == "preserved"
    auth.delete("trajectory-test-auth")


def test_large_real_replay_is_identical_and_computed_once():
    count = 1000
    costs = np.column_stack((np.linspace(0, 300, count), np.linspace(0, 600, count)))
    model = DynamicModelInput(
        times_hours=np.linspace(0, 168, count),
        costs_usd=costs,
        displayed_percent=np.floor(costs.sum(axis=1) / 3000 * 100),
        rights_percent=np.array([50.0, 50.0]),
    )
    args = dict(
        account_id=7,
        algorithm="large-test",
        seed=123,
        config=ParticleFilterConfig(),
        capacity_profile=PRO_20X_CAPACITY_PROFILE,
    )
    compute = Mock(wraps=run_adaptive_range_filter)
    began = perf_counter()
    fresh = cache_module.cached_replay(model, **args, compute=compute)
    cold = perf_counter() - began
    began = perf_counter()
    reused = cache_module.cached_replay(model, **args, compute=compute)
    warm = perf_counter() - began
    assert compute.call_count == 1
    for name in (
        "total_percent_hat",
        "capacity_hat_usd",
        "capacity_lower_usd",
        "capacity_upper_usd",
        "capacity_particle_samples_usd",
    ):
        np.testing.assert_array_equal(
            getattr(fresh.particle, name), getattr(reused.particle, name)
        )
    np.testing.assert_array_equal(fresh.stage, reused.stage)
    assert fresh.promotions == reused.promotions
    print(f"\n{count}-point numerical replay: cold {cold:.3f}s; cache hit {warm:.3f}s")


def test_expired_artifacts_are_recomputed(inputs, monkeypatch):
    model, args = inputs
    monkeypatch.setattr(cache_module, "CACHE_SECONDS", 0)
    compute = Mock(return_value="expired")
    cache_module.cached_replay(model, **args, compute=compute)
    cache_module.cached_replay(model, **args, compute=compute)
    assert compute.call_count == 2


@pytest.mark.django_db
def test_cached_trajectory_keeps_names_permissions_and_history_fresh(setup):
    config, _, account, alice, bob, _, start = setup
    first = observation(account, start, start, 0, 0)
    second = observation(account, start, start + timedelta(hours=1), 10, 100)
    for item in (first, second):
        item.attribution_started_at = start
        item.save()
        for person in (alice, bob):
            ParticipantSnapshot.objects.create(
                observation=item,
                participant=person,
                share_percent=50,
                raw_selected_cost=0 if item == first else 30,
                selected_cost=0 if item == first else 30,
            )
    with patch(
        "monitor.particle_trajectory.run_adaptive_range_filter",
        wraps=run_adaptive_range_filter,
    ) as compute:
        began = perf_counter()
        with CaptureQueriesContext(connection) as queries:
            admin_data = particle_trajectory_data(config, account)
        cold = perf_counter() - began
        began = perf_counter()
        member_data = particle_trajectory_data(
            config, account, allowed_participant_ids={alice.id}
        )
        warm = perf_counter() - began
        assert compute.call_count == 1
        assert admin_data["points"] == member_data["points"]
        assert len(admin_data["cycle_usage"]["participants"]) == 2
        assert [
            row["participant_id"] for row in member_data["cycle_usage"]["participants"]
        ] == [alice.id]
        alice.name = "renamed"
        alice.save()
        account.name = "new account name"
        account.save()
        fresh = particle_trajectory_data(
            config, account, allowed_participant_ids={bob.id}
        )
        assert compute.call_count == 1
        assert fresh["account"]["name"] == "new account name"
        assert fresh["cycle_usage"]["participants"][0]["participant_id"] == bob.id
        renamed = particle_trajectory_data(
            config, account, allowed_participant_ids={alice.id}
        )
        assert (
            renamed["cycle_usage"]["participants"][0]["participant_name"] == "renamed"
        )
        # Editing a sample, without changing count/latest ID, invalidates.
        second.total_actual_cost = 200
        second.total_standard_cost = 200
        second.save()
        particle_trajectory_data(config, account)
        assert compute.call_count == 2
    assert all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)
    print(f"\nTrajectory replay: cold {cold:.3f}s; unchanged input {warm:.3f}s")
