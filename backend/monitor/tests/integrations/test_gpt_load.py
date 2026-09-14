from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from monitor.cpa.participants import owner_index, record_contract
from monitor.cpa.reporting import account_summary
from monitor.cpa.usage import cpa_event_cost
from monitor.gpt_load.usage import (
    access_key_hash,
    event_fingerprint,
    sync_access_keys,
    sync_usage,
)
from monitor.history_state import LeaseGuard
from monitor.integrations.gpt_load import GPTLoadClient, GPTLoadError
from monitor.models import (
    AppSettings,
    CPAAPIKey,
    CPAAccountCollectionInterval,
    CPAKeyBinding,
    CPAUsageEvent,
    MonitoredAccount,
    Observation,
    Participant,
    ParticipantSnapshot,
    PoolParticipant,
    UsageSamplePoint,
)
from monitor.tests.helpers import create_cpa_account, create_monitored_account, jwt_login


def _client(handler) -> GPTLoadClient:
    config = SimpleNamespace(
        gpt_load_base_url="https://gpt-load.example",
        gpt_load_auth_key_encrypted="",
        request_timeout_seconds=10,
        verify_tls=True,
    )
    client = GPTLoadClient(config, auth_key="management-secret")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer management-secret"},
    )
    return client


def test_gpt_load_client_parses_envelopes_and_all_pagination_modes():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer management-secret"
        path = request.url.path
        page = request.url.params.get("page")
        cursor = request.url.params.get("cursor")
        if path == "/api/home":
            data = {"version": "1.2.3", "inventory": {"groups": 2}}
        elif path == "/api/groups" and page == "1":
            data = {
                "items": [
                    {
                        "id": 11,
                        "name": "Codex Group",
                        "channel_id": "openai",
                        "connection_type": "subscription",
                    }
                ],
                "pagination": {"total_pages": 2},
            }
        elif path == "/api/groups" and page == "2":
            data = {
                "items": [
                    {
                        "id": 12,
                        "name": "API Group",
                        "connection_type": "api_key",
                    }
                ],
                "pagination": {"total_pages": 2},
            }
        elif path == "/api/groups/11/credentials" and page == "1":
            data = {
                "items": [
                    {
                        "credential_id": 21,
                        "mask": "acct...demo",
                        "account": {"email": "owner@example.com"},
                        "configured_status": "active",
                        "effective_status": "available",
                        "observation": {
                            "snapshot": {"plan_summary": {"name": "Pro"}}
                        },
                    }
                ],
                "pagination": {"total_pages": 1},
            }
        elif path == "/api/logs" and cursor is None:
            data = {"items": [{"request_id": "first"}], "next_cursor": "next"}
        elif path == "/api/logs" and cursor == "next":
            data = {"items": [{"request_id": "second"}], "next_cursor": None}
        else:
            raise AssertionError(f"unexpected request: {request.url}")
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": data})

    with _client(handler) as client:
        assert client.test_connection() == {
            "connected": True,
            "version": "1.2.3",
            "inventory": {"groups": 2},
        }
        assert client.list_source_accounts() == [
            {
                "group_id": 11,
                "group_name": "Codex Group",
                "channel_id": "openai",
                "credential_id": 21,
                "email": "owner@example.com",
                "mask": "acct...demo",
                "plan_type": "Pro",
                "configured_status": "active",
                "effective_status": "available",
            }
        ]
        assert [
            row["request_id"]
            for row in client.iter_logs(
                group_id=11,
                credential_id=21,
                from_ms=1_000,
                to_ms=2_000,
            )
        ] == ["first", "second"]

    log_requests = [request for request in requests if request.url.path == "/api/logs"]
    assert len(log_requests) == 2
    assert log_requests[0].url.params["group_id"] == "11"
    assert log_requests[0].url.params["credential_id"] == "21"
    assert log_requests[1].url.params["cursor"] == "next"


def test_gpt_load_client_reads_official_weekly_window_and_rejects_error_envelope():
    observed_at = 1_800_000_000_000

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/observation-refresh"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "message": "ok",
                    "data": {
                        "state": "fresh",
                        "observed_at_ms": observed_at,
                        "snapshot": {
                            "plan_summary": {"name": "Pro"},
                            "quota_windows": [
                                {
                                    "id": "five-hour",
                                    "scope": "account",
                                    "window_seconds": 18_000,
                                    "reset_at_ms": observed_at + 10_000,
                                    "used": 50,
                                },
                                {
                                    "id": "seven-day",
                                    "scope": "account",
                                    "window_seconds": 604_800,
                                    "reset_at_ms": observed_at + 86_400_000,
                                    "used": "37.5",
                                },
                            ],
                        },
                    },
                },
            )
        return httpx.Response(
            401,
            json={"code": "unauthorized", "message": "invalid key"},
        )

    with _client(handler) as client:
        window = client.query_weekly_window(11, 21)
        assert window.used_percent == Decimal("37.5")
        assert window.window_seconds == 604_800
        assert window.reset_after_seconds == 86_400
        assert window.plan_type == "Pro"
        with pytest.raises(GPTLoadError, match="HTTP 401") as captured:
            client.test_connection()
        assert "management-secret" not in str(captured.value)


def _log(when, *, request_id="00000000-0000-4000-8000-000000000001"):
    return {
        "request_id": request_id,
        "completed_at_ms": int(when.timestamp() * 1_000),
        "access_key": {"id": 71, "name": "Alice"},
        "protocol": "openai-responses",
        "operation": "responses",
        "upstream_protocol": "openai-responses",
        "client_model": "gpt-5",
        "upstream_model": "gpt-5",
        "reasoning": None,
        "status": "success",
        "first_response_ms": None,
        "duration_ms": 1_200,
        "credential_name": "subscription-owner",
        "usage_state": "complete",
        "cost_state": "priced",
        "pricing_completeness": "complete",
        "pricing_mode": "standard",
        "input_tokens": "100",
        "cache_read_tokens": "20",
        "output_tokens": "30",
        "estimated_cost_nano_usd": "2000000000",
    }


@pytest.mark.django_db
def test_gpt_load_sync_is_idempotent_skips_precutover_and_keeps_member_cycle_total():
    config = AppSettings.load()
    config.cpa_model_pricing = {
        "legacy-model": {"input": "10", "cached_input": "1", "output": "30"}
    }
    config.save()
    account = create_cpa_account()
    now = timezone.now()
    cycle_start = now - timedelta(days=2)
    cutover = now - timedelta(hours=1)
    account.created_at = cycle_start
    account.provider = "gpt_load"
    account.gpt_load_group_id = 11
    account.gpt_load_credential_id = 21
    account.gpt_load_cutover_at = cutover
    account.save()

    participant = Participant.objects.create(name="Alice")
    PoolParticipant.objects.create(
        pool=account.pool,
        participant=participant,
        share_percent=100,
    )
    legacy_key = CPAAPIKey.objects.create(
        key_hash="a" * 64,
        hint="old1",
        source="cpa",
    )
    CPAKeyBinding.objects.create(
        key=legacy_key,
        participant=participant,
        started_at=cycle_start,
    )
    legacy_event = CPAUsageEvent.objects.create(
        account=account,
        event_fingerprint="legacy-event",
        request_id="legacy-request",
        source="cpa",
        occurred_at=cutover - timedelta(minutes=10),
        model="legacy-model",
        api_key_hash=legacy_key.key_hash,
        api_key_hint=legacy_key.hint,
        input_tokens=1_000_000,
        total_tokens=1_000_000,
    )
    legacy_values = CPAUsageEvent.objects.filter(pk=legacy_event.pk).values().get()

    class FakeClient:
        def list_access_keys(self):
            return [{"id": 71, "name": "Alice", "masked_key": "sk-gl-...new1"}]

        def iter_logs(self, **kwargs):
            self.last_query = kwargs
            return iter(
                [
                    _log(cutover - timedelta(seconds=1), request_id="before"),
                    _log(cutover + timedelta(minutes=10)),
                ]
            )

    fake = FakeClient()
    synced_key = sync_access_keys(fake)[71]
    CPAKeyBinding.objects.create(
        key=synced_key,
        participant=participant,
        started_at=cutover,
    )
    through = now
    guard = LeaseGuard.acquire(account.fact_key)
    try:
        first = sync_usage(config, account, fake, guard, through=through)
        second = sync_usage(
            config,
            account,
            fake,
            guard,
            through=through + timedelta(minutes=1),
        )
    finally:
        guard.release()

    assert first == {"created": 1, "duplicates": 0}
    assert second == {"created": 0, "duplicates": 1}
    assert CPAUsageEvent.objects.filter(source="gpt_load").count() == 1
    assert not CPAUsageEvent.objects.filter(request_id="before").exists()
    assert CPAUsageEvent.objects.filter(pk=legacy_event.pk).values().get() == legacy_values
    imported = CPAUsageEvent.objects.get(source="gpt_load")
    assert imported.event_fingerprint == event_fingerprint(imported.request_id)
    assert imported.api_key_hash == access_key_hash(71)
    assert imported.ttft_ms == 0
    assert cpa_event_cost(imported, config) == (Decimal("2"), False)

    Observation.objects.create(
        account_id=account.fact_key,
        observed_at=through,
        upstream_resets_at=cycle_start + timedelta(days=7),
        window_seconds=604_800,
        upstream_used_percent=Decimal("30"),
        interval_used_percent=Decimal("30"),
        raw_selected_total_cost=Decimal("12"),
        selected_total_cost=Decimal("12"),
        total_standard_cost=Decimal("12"),
        total_actual_cost=Decimal("12"),
        effective_usd_per_percent=Decimal("0.4"),
        cost_window_started_at=cycle_start,
        cost_window_ended_at=through,
        raw_window={"provider": "gpt_load"},
    )
    _observation, _start, totals, total, _coverage, _snapshots, _latest = (
        account_summary(account, config, through, owner_index())
    )
    assert totals[participant.id]["usage_usd"] == Decimal("12")
    assert total["usage_usd"] == Decimal("12")


@pytest.mark.django_db
def test_gpt_load_key_sync_preserves_existing_hint_when_log_reference_is_sparse():
    key = CPAAPIKey.objects.create(
        source="gpt_load",
        external_key_id=71,
        key_hash=access_key_hash(71),
        hint="BEEF",
        name="Old",
    )

    class FakeClient:
        def list_access_keys(self):
            return []

    sync_access_keys(FakeClient(), [{"id": 71, "name": "Renamed"}])

    key.refresh_from_db()
    assert key.hint == "BEEF"
    assert key.name == "Renamed"


@pytest.mark.django_db
def test_gpt_load_access_keys_are_synced_and_only_bound_by_remote_identity(
    monkeypatch,
):
    get_user_model().objects.create_superuser(
        "owner",
        "owner@example.com",
        "very-strong-password",
    )
    participant = Participant.objects.create(name="Alice")
    client = Client()
    headers, _response = jwt_login(client)

    class FailingClient:
        def __init__(self, _config):
            raise GPTLoadError("gateway unavailable")

    monkeypatch.setattr(
        "monitor.integrations.gpt_load.GPTLoadClient",
        FailingClient,
    )
    failed = client.get("/api/cpa/keys?provider=gpt_load", **headers)
    assert failed.status_code == 502

    class FakeClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_access_keys(self):
            return [
                {"id": 71, "name": "Alice laptop", "masked_key": "sk-gl-...BEEF"}
            ]

    monkeypatch.setattr(
        "monitor.integrations.gpt_load.GPTLoadClient",
        FakeClient,
    )
    listed = client.get("/api/cpa/keys?provider=gpt_load", **headers)
    assert listed.status_code == 200, listed.json()
    remote_key = listed.json()["data"]["keys"][0]
    assert remote_key["name"] == "Alice laptop"
    assert remote_key["hint"] == "BEEF"

    raw_rejected = client.post(
        "/api/cpa/keys?provider=gpt_load",
        data=json.dumps(
            {"participant_id": participant.id, "raw_key": "secret-key"}
        ),
        content_type="application/json",
        **headers,
    )
    assert raw_rejected.status_code == 400

    bound = client.post(
        "/api/cpa/keys?provider=gpt_load",
        data=json.dumps(
            {
                "participant_id": participant.id,
                "observed_hash": remote_key["observed_hash"],
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert bound.status_code == 201, bound.json()
    assert participant.cpa_bindings.get().key.source == "gpt_load"


@pytest.mark.django_db
def test_cpa_cutover_preserves_fact_identity_and_historical_rows(monkeypatch):
    get_user_model().objects.create_superuser(
        "owner",
        "owner@example.com",
        "very-strong-password",
    )
    client = Client()
    headers, _response = jwt_login(client)
    account = create_cpa_account("legacy-auth", name="Legacy CPA")
    now = timezone.now()
    cycle_start = now - timedelta(days=2)
    account.created_at = cycle_start
    account.save(update_fields=["created_at"])
    participant = Participant.objects.create(name="Alice")
    PoolParticipant.objects.create(
        pool=account.pool,
        participant=participant,
        share_percent=100,
    )
    record_contract(account, cycle_start)
    CPAAccountCollectionInterval.objects.create(
        account=account,
        session_key="legacy-cpa-session",
        connected_at=cycle_start,
    )
    event = CPAUsageEvent.objects.create(
        account=account,
        event_fingerprint="legacy-cutover-event",
        occurred_at=now - timedelta(hours=2),
        model="legacy-model",
    )
    observation = Observation.objects.create(
        account_id=account.fact_key,
        observed_at=now - timedelta(hours=2),
        upstream_resets_at=cycle_start + timedelta(days=7),
        upstream_used_percent=Decimal("20"),
        raw_selected_total_cost=Decimal("1"),
        selected_total_cost=Decimal("1"),
        total_standard_cost=Decimal("1"),
        total_actual_cost=Decimal("1"),
        effective_usd_per_percent=Decimal("0.05"),
        raw_window={"provider": "cpa"},
    )
    snapshot = ParticipantSnapshot.objects.create(
        observation=observation,
        participant=participant,
        share_percent=100,
        quota_pool_id=account.pool_id,
        quota_pool_name=account.pool.name,
        pool_contract_revision=account.pool.contract_revision,
        selected_cost=Decimal("1"),
        raw_selected_cost=Decimal("1"),
    )
    event_before = CPAUsageEvent.objects.filter(pk=event.pk).values().get()
    observation_before = Observation.objects.filter(pk=observation.pk).values().get()
    snapshot_before = ParticipantSnapshot.objects.filter(pk=snapshot.pk).values().get()
    contracts_before = list(account.cpa_contracts.order_by("pk").values())
    original_pk = account.pk
    original_fact_key = account.fact_key
    original_pool_id = account.pool_id

    class FakeGPTLoadClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_source_accounts(self):
            return [
                {
                    "group_id": 11,
                    "group_name": "Codex",
                    "credential_id": 21,
                    "email": "owner@example.com",
                }
            ]

    monkeypatch.setattr("monitor.views.settings.GPTLoadClient", FakeGPTLoadClient)

    response = client.post(
        f"/api/settings/monitored-accounts/{account.pk}/gpt-load-cutover",
        data=json.dumps({"group_id": 11, "credential_id": 21}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 200, response.json()
    account.refresh_from_db()
    assert account.pk == original_pk
    assert account.fact_key == original_fact_key
    assert account.pool_id == original_pool_id
    assert account.provider == "gpt_load"
    assert account.cpa_auth_index == "legacy-auth"
    assert account.gpt_load_group_id == 11
    assert account.gpt_load_credential_id == 21
    assert account.gpt_load_cutover_at is not None
    assert CPAUsageEvent.objects.filter(pk=event.pk).values().get() == event_before
    assert Observation.objects.filter(pk=observation.pk).values().get() == observation_before
    assert ParticipantSnapshot.objects.filter(pk=snapshot.pk).values().get() == snapshot_before
    assert list(account.cpa_contracts.order_by("pk").values()) == contracts_before

    allocation = client.get("/api/quota-allocation?provider=gpt_load", **headers)
    assert allocation.status_code == 200, allocation.json()
    allocation_data = allocation.json()["data"]
    assert [row["id"] for row in allocation_data["accounts"]] == [account.id]
    continued_pool = next(
        row for row in allocation_data["pools"] if row["id"] == original_pool_id
    )
    assert continued_pool["account_ids"] == [account.id]
    assert continued_pool["allocations"] == [
        {"participant_id": participant.id, "share_percent": 100.0}
    ]

    intervals = list(account.cpa_collection_intervals.order_by("connected_at"))
    assert len(intervals) == 2
    assert intervals[0].disconnected_at == intervals[1].connected_at
    assert intervals[0].end_reliable is True
    assert intervals[1].disconnected_at is None

    sub2api = create_monitored_account(9001)
    rejected = client.post(
        f"/api/settings/monitored-accounts/{sub2api.pk}/gpt-load-cutover",
        data=json.dumps({"group_id": 11, "credential_id": 21}),
        content_type="application/json",
        **headers,
    )
    assert rejected.status_code == 409
    sub2api.refresh_from_db()
    assert sub2api.provider == "sub2api"

    created = client.post(
        "/api/settings/monitored-accounts",
        data=json.dumps(
            {
                "provider": "gpt_load",
                "gpt_load_group_id": 11,
                "gpt_load_credential_id": 22,
                "name": "New GPT-Load",
                "enabled": True,
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert created.status_code == 201, created.json()
    new_account = MonitoredAccount.objects.get(pk=created.json()["data"]["id"])
    assert new_account.gpt_load_cutover_at is not None
    interval = new_account.cpa_collection_intervals.get()
    assert interval.connected_at == new_account.gpt_load_cutover_at
    assert interval.disconnected_at is None


@pytest.mark.django_db
def test_cpa_cutover_absorbs_accidentally_created_independent_account(monkeypatch):
    get_user_model().objects.create_superuser(
        "owner",
        "owner@example.com",
        "very-strong-password",
    )
    client = Client()
    headers, _response = jwt_login(client)
    account = create_cpa_account("legacy-auth", name="Original subscription")
    config = AppSettings.load()
    config.cpa_model_pricing = {
        "legacy-model": {"input": "10", "cached_input": "1", "output": "30"}
    }
    config.save()
    now = timezone.now()
    cycle_start = now - timedelta(days=2)
    account.created_at = cycle_start
    account.save(update_fields=["created_at"])
    original_pk = account.pk
    original_pool_id = account.pool_id

    participant = Participant.objects.create(name="Alice")
    PoolParticipant.objects.create(
        pool=account.pool,
        participant=participant,
        share_percent=100,
    )
    record_contract(account, cycle_start)
    CPAAccountCollectionInterval.objects.create(
        account=account,
        session_key="legacy-cpa-session",
        connected_at=cycle_start,
    )
    legacy_key = CPAAPIKey.objects.create(
        source="cpa",
        key_hash="a" * 64,
        hint="old1",
    )
    CPAKeyBinding.objects.create(
        key=legacy_key,
        participant=participant,
        started_at=cycle_start,
    )
    legacy_event = CPAUsageEvent.objects.create(
        account=account,
        event_fingerprint="legacy-before-accidental-account",
        request_id="legacy-request",
        source="cpa",
        occurred_at=now - timedelta(minutes=20),
        model="legacy-model",
        api_key_hash=legacy_key.key_hash,
        api_key_hint=legacy_key.hint,
        input_tokens=1_000_000,
        total_tokens=1_000_000,
    )
    legacy_observation = Observation.objects.create(
        account_id=account.fact_key,
        observed_at=now - timedelta(minutes=15),
        upstream_resets_at=cycle_start + timedelta(days=7),
        upstream_used_percent=Decimal("20"),
        raw_selected_total_cost=Decimal("1"),
        selected_total_cost=Decimal("1"),
        total_standard_cost=Decimal("1"),
        total_actual_cost=Decimal("1"),
        effective_usd_per_percent=Decimal("0.05"),
        raw_window={"provider": "cpa"},
    )

    created = client.post(
        "/api/settings/monitored-accounts",
        data=json.dumps(
            {
                "provider": "gpt_load",
                "gpt_load_group_id": 11,
                "gpt_load_credential_id": 21,
                "name": "Accidental independent account",
                "enabled": True,
            }
        ),
        content_type="application/json",
        **headers,
    )
    assert created.status_code == 201, created.json()
    accidental = MonitoredAccount.objects.get(pk=created.json()["data"]["id"])
    accidental_pk = accidental.pk
    accidental_pool_id = accidental.pool_id
    cutover_at = accidental.gpt_load_cutover_at
    assert cutover_at is not None

    imported_key = CPAAPIKey.objects.create(
        source="gpt_load",
        external_key_id=71,
        key_hash=access_key_hash(71),
        hint="new1",
    )
    CPAKeyBinding.objects.create(
        key=imported_key,
        participant=participant,
        started_at=cutover_at,
    )
    imported_at = timezone.now()
    imported_event = CPAUsageEvent.objects.create(
        account=accidental,
        event_fingerprint="gpt-load-after-accidental-account",
        request_id="gpt-load-request",
        source="gpt_load",
        source_cost_nano_usd=2_000_000_000,
        occurred_at=imported_at,
        model="gpt-5",
        api_key_hash=imported_key.key_hash,
        api_key_hint=imported_key.hint,
    )
    point = UsageSamplePoint.objects.create(
        account_id=accidental.fact_key,
        observed_at=imported_at,
    )
    imported_observation = Observation.objects.create(
        account_id=accidental.fact_key,
        sample_point=point,
        observed_at=imported_at,
        upstream_resets_at=cycle_start + timedelta(days=7),
        upstream_used_percent=Decimal("21"),
        raw_selected_total_cost=Decimal("2"),
        selected_total_cost=Decimal("2"),
        total_standard_cost=Decimal("2"),
        total_actual_cost=Decimal("2"),
        effective_usd_per_percent=Decimal("0.05"),
        raw_window={"provider": "gpt_load"},
    )
    record_contract(accidental, cutover_at)

    class FakeGPTLoadClient:
        def __init__(self, _config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def list_source_accounts(self):
            return [
                {
                    "group_id": 11,
                    "group_name": "Codex",
                    "credential_id": 21,
                    "email": "owner@example.com",
                }
            ]

    monkeypatch.setattr("monitor.views.settings.GPTLoadClient", FakeGPTLoadClient)

    response = client.post(
        f"/api/settings/monitored-accounts/{account.pk}/gpt-load-cutover",
        data=json.dumps({"group_id": 11, "credential_id": 21}),
        content_type="application/json",
        **headers,
    )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["absorbed_account_id"] == accidental_pk
    assert not MonitoredAccount.objects.filter(pk=accidental_pk).exists()
    assert not account.__class__.objects.filter(pool_id=accidental_pool_id).exists()

    account.refresh_from_db()
    assert account.pk == original_pk
    assert account.pool_id == original_pool_id
    assert account.provider == "gpt_load"
    assert account.cpa_auth_index == "legacy-auth"
    assert account.gpt_load_group_id == 11
    assert account.gpt_load_credential_id == 21
    assert account.gpt_load_cutover_at == cutover_at
    assert set(account.cpa_usage_events.values_list("pk", flat=True)) == {
        legacy_event.pk,
        imported_event.pk,
    }
    legacy_observation.refresh_from_db()
    imported_observation.refresh_from_db()
    point.refresh_from_db()
    assert legacy_observation.account_id == account.fact_key
    assert imported_observation.account_id == account.fact_key
    assert point.account_id == account.fact_key
    assert imported_observation.raw_selected_total_cost >= Decimal("2")
    assert list(account.pool.allocations.values_list("participant_id", "share_percent")) == [
        (participant.id, Decimal("100"))
    ]
    assert all(
        contract.allocations
        == [{"participant_id": participant.id, "share_percent": "100.000"}]
        for contract in account.cpa_contracts.all()
    )
    intervals = list(account.cpa_collection_intervals.order_by("connected_at"))
    assert len(intervals) == 2
    assert intervals[0].disconnected_at == cutover_at
    assert intervals[1].connected_at == cutover_at

    _observation, _start, totals, total, _coverage, _snapshots, _latest = (
        account_summary(account, config, imported_at, owner_index())
    )
    assert totals[participant.id]["usage_usd"] == Decimal("12")
    assert total["usage_usd"] == Decimal("12")

    requests = client.get(
        f"/api/cpa/requests?account_id={account.id}&days=7",
        **headers,
    )
    assert requests.status_code == 200, requests.json()
    request_rows = requests.json()["data"]["items"]
    assert {row["request_id"] for row in request_rows} == {
        "legacy-request",
        "gpt-load-request",
    }
    assert {row["source"] for row in request_rows} == {"cpa", "gpt_load"}
