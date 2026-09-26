from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from monitor.cpa.billing import billing_summary
from monitor.cpa.participants import owner_index, record_contract
from monitor.cpa.quota_discrepancy import (
    apply_adjustment,
    preview_adjustment,
    quota_discrepancies,
    reverse_adjustment,
)
from monitor.cpa.reporting import pool_summary
from monitor.models import CPAAccountCollectionInterval, Observation
from monitor.tests.helpers import jwt_login

from .test_cpa_participants import (
    event,
    member_client,
    observation,
    setup,  # noqa: F401
)

pytestmark = pytest.mark.django_db


def gpt_load_discrepancy(setup):
    config, admin, account, alice, bob, keys, start = setup
    config.weekly_quota_model = "time_varying"
    config.save()
    cutover = start + timedelta(minutes=10)

    calibrated = observation(account, start, cutover - timedelta(minutes=1), 10, 100)
    calibrated.attribution_started_at = start
    calibrated.interval_used_percent = 10
    calibrated.valid_sample = True
    calibrated.model_diagnostics = {"algorithm": "particle_filter_test"}
    calibrated.capacity_lower_usd = 900
    calibrated.capacity_upper_usd = 1100
    calibrated.effective_usd_per_percent = 10
    calibrated.save()

    account.provider = "gpt_load"
    account.cpa_auth_index = None
    account.gpt_load_group_id = 7
    account.gpt_load_credential_id = 11
    account.gpt_load_cutover_at = cutover
    account.save()
    CPAAccountCollectionInterval.objects.create(
        account=account,
        session_key="gpt-load-discrepancy",
        connected_at=cutover,
    )

    baseline = observation(account, start, cutover + timedelta(minutes=1), 10, 0)
    baseline.raw_window = {"provider": "gpt_load"}
    baseline.save()
    logged = event(account, keys[1], cutover + timedelta(minutes=20))
    logged.source = "gpt_load"
    logged.source_cost_nano_usd = 10_000_000_000
    logged.usage_state = "complete"
    logged.cost_state = "priced"
    logged.pricing_completeness = "complete"
    logged.save()
    latest = observation(account, start, cutover + timedelta(hours=1), 30, 10)
    latest.raw_window = {"provider": "gpt_load"}
    latest.save()
    account.gpt_load_logs_synced_through = latest.observed_at
    account.save(update_fields=["gpt_load_logs_synced_through"])
    return config, admin, account, alice, bob, keys, start, baseline, latest


def test_gpt_load_discrepancy_freezes_lower_bound_without_charging_owner(setup):
    config, admin, account, alice, bob, _, _, baseline, latest = (
        gpt_load_discrepancy(setup)
    )
    row = quota_discrepancies(account, config, latest.observed_at)[0]
    assert row.baseline_observation_id == baseline.id
    assert row.request_usage_usd == 10
    assert row.suggested_usd == 190
    assert row.lower_usd == 170
    assert row.upper_usd == 210
    assert row.held_unexplained_usd == 170
    assert row.member_holds == {alice.id: 85, bob.id: 85}
    assert row.member_adjustments == {}
    assert row.can_attribute

    summary = pool_summary(admin, account, config)
    members = {item["participant_id"]: item for item in summary["members"]}
    public_discrepancy = summary["accounts"][0]["quota_discrepancy"]
    assert members[alice.id]["usage_usd"] == 0
    assert members[alice.id]["request_usage_usd"] == 0
    assert members[alice.id]["held_unexplained_usd"] == 85
    assert members[bob.id]["usage_usd"] == 10
    assert members[bob.id]["request_count"] == 1
    assert public_discrepancy["suggested_usd"] == 190
    assert "latest_observation_id" not in public_discrepancy
    assert "eligible_participant_ids" not in public_discrepancy


def test_manual_adjustment_is_idempotent_and_reversible(setup):
    config, admin, account, alice, bob, _, _, _, latest = gpt_load_discrepancy(
        setup
    )
    plan = preview_adjustment(
        account=account,
        participant=alice,
        latest_observation_id=latest.id,
        amount_usd=Decimal("190"),
        reason="车主确认本周期有账号直连",
        user=admin,
    )
    adjustment = apply_adjustment(plan.id)
    assert apply_adjustment(plan.id).id == adjustment.id
    assert adjustment.amount_usd == 190

    summary = pool_summary(admin, account, config)
    members = {item["participant_id"]: item for item in summary["members"]}
    assert members[alice.id]["usage_usd"] == 190
    assert members[alice.id]["manual_adjustment_usd"] == 190
    assert members[alice.id]["request_count"] == 0
    assert members[alice.id]["token_count"] == 0
    assert members[alice.id]["held_unexplained_usd"] == 0
    assert members[bob.id]["usage_usd"] == 10

    reversal = reverse_adjustment(
        adjustment.id,
        reason="管理员复核后撤销",
        user=admin,
    )
    assert reversal.amount_usd == -190
    assert reverse_adjustment(
        adjustment.id,
        reason="重复撤销保持幂等",
        user=admin,
    ).id == reversal.id
    row = quota_discrepancies(account, config, latest.observed_at)[0]
    assert row.manual_adjustment_usd == 0
    assert row.held_unexplained_usd == 170


def test_discrepancy_requires_synced_logs_and_calibrated_prebaseline_capacity(setup):
    config, _, account, _, _, _, _, _, latest = gpt_load_discrepancy(setup)
    account.gpt_load_logs_synced_through = latest.observed_at - timedelta(seconds=1)
    account.save(update_fields=["gpt_load_logs_synced_through"])
    row = quota_discrepancies(account, config, latest.observed_at)[0]
    assert not row.can_attribute
    assert row.held_unexplained_usd == 0
    assert "GPT-Load 请求日志尚未同步到额度观测" in row.reasons


def test_first_gpt_load_observation_is_only_a_baseline(setup):
    config, _, account, _, _, _, _, baseline, _ = gpt_load_discrepancy(setup)
    account.gpt_load_logs_synced_through = baseline.observed_at
    account.save(update_fields=["gpt_load_logs_synced_through"])
    row = quota_discrepancies(account, config, baseline.observed_at)[0]
    assert row.official_delta_percent == 0
    assert row.suggested_usd == 0
    assert row.held_unexplained_usd == 0
    assert not row.can_attribute


def test_discrepancy_without_calibrated_capacity_stays_unknown(setup):
    config, _, account, _, _, _, _, baseline, latest = gpt_load_discrepancy(setup)
    Observation.objects.filter(
        account_id=account.fact_key, observed_at__lt=baseline.observed_at
    ).update(
        attribution_started_at=None,
        valid_sample=False,
        model_diagnostics={},
        capacity_lower_usd=None,
        capacity_upper_usd=None,
    )

    row = quota_discrepancies(account, config, latest.observed_at)[0]
    assert not row.can_attribute
    assert row.held_unexplained_usd == 0
    assert row.suggested_usd == 0
    assert "基线前没有已校准容量区间" in row.reasons


def test_adjustment_preview_is_invalidated_when_evidence_changes(setup):
    _, admin, account, alice, _, keys, _, _, latest = gpt_load_discrepancy(setup)
    plan = preview_adjustment(
        account=account,
        participant=alice,
        latest_observation_id=latest.id,
        amount_usd=Decimal("50"),
        reason="先生成预览",
        user=admin,
    )
    changed = event(account, keys[0], latest.observed_at - timedelta(minutes=5))
    changed.source = "gpt_load"
    changed.save(update_fields=["source"])

    with pytest.raises(ValidationError, match="预览已过期或证据已变化"):
        apply_adjustment(plan.id)


def test_unrelated_account_health_update_does_not_invalidate_preview(setup):
    _, admin, account, alice, _, _, _, _, latest = gpt_load_discrepancy(setup)
    plan = preview_adjustment(
        account=account,
        participant=alice,
        latest_observation_id=latest.id,
        amount_usd=Decimal("1"),
        reason="健康状态变化不属于额度证据",
        user=admin,
    )
    account.last_success_at = timezone.now()
    account.last_error = "temporary status"
    account.save(update_fields=["last_success_at", "last_error", "updated_at"])

    assert apply_adjustment(plan.id).amount_usd == 1


def test_cycle_contract_not_current_allocations_controls_hold_and_eligibility(setup):
    config, admin, account, alice, bob, _, _, _, latest = gpt_load_discrepancy(
        setup
    )
    account.pool.allocations.filter(participant=alice).delete()
    bob_allocation = account.pool.allocations.get(participant=bob)
    bob_allocation.share_percent = 100
    bob_allocation.save(update_fields=["share_percent"])
    account.pool.contract_revision += 1
    account.pool.save(update_fields=["contract_revision"])
    record_contract(account, latest.observed_at + timedelta(minutes=1))

    row = quota_discrepancies(account, config, latest.observed_at)[0]
    assert row.member_holds == {alice.id: 85, bob.id: 85}
    assert alice.id in row.eligible_participant_ids
    assert preview_adjustment(
        account=account,
        participant=alice,
        latest_observation_id=latest.id,
        amount_usd=Decimal("1"),
        reason="归因给周期历史合同成员",
        user=admin,
    )


def test_manual_adjustment_and_hold_are_separate_in_billing_period(setup):
    config, admin, account, alice, bob, _, _, _, latest = gpt_load_discrepancy(
        setup
    )
    account.pool.cpa_billing_anchor = (latest.observed_at - timedelta(days=1)).date()
    account.pool.cpa_billing_timezone = "UTC"
    account.pool.save(update_fields=["cpa_billing_anchor", "cpa_billing_timezone"])
    plan = preview_adjustment(
        account=account,
        participant=alice,
        latest_observation_id=latest.id,
        amount_usd=Decimal("50"),
        reason="账期内确认直连用量",
        user=admin,
    )
    apply_adjustment(plan.id)

    result = billing_summary(
        admin,
        account,
        config,
        latest.observed_at + timedelta(minutes=1),
        owner_index(),
        {alice.id: alice, bob.id: bob},
    )
    members = {row["participant_id"]: row for row in result["members"]}
    assert result["usage_usd"] == 60
    assert result["request_usage_usd"] == 10
    assert result["manual_adjustment_usd"] == 50
    assert result["held_unexplained_usd"] == 120
    assert members[alice.id]["usage_usd"] == 50
    assert members[alice.id]["held_unexplained_usd"] == 60
    assert members[bob.id]["request_usage_usd"] == 10
    assert members[bob.id]["held_unexplained_usd"] == 60


def test_quota_adjustment_admin_api_and_permissions(setup):
    _, _, account, alice, _, _, _, _, latest = gpt_load_discrepancy(setup)
    _, member, member_headers = member_client(account, alice)
    url = f"/api/cpa/quota-discrepancies?account_id={account.id}"
    assert member.get(url, **member_headers).status_code == 403

    client = Client()
    headers, _ = jwt_login(client, "owner")
    response = client.get(url, **headers)
    assert response.status_code == 200
    assert response.json()["data"]["cycles"][0]["suggested_usd"] == 190

    preview_url = f"/api/cpa/quota-adjustments/preview?account_id={account.id}"
    too_large = {
        "participant_id": alice.id,
        "latest_observation_id": latest.id,
        "amount_usd": 211,
        "reason": "超过上限",
    }
    assert (
        client.post(
            preview_url, too_large, content_type="application/json", **headers
        ).status_code
        == 400
    )
    payload = {**too_large, "amount_usd": 190, "reason": "管理员已核对直连记录"}
    preview = client.post(
        preview_url, payload, content_type="application/json", **headers
    )
    assert preview.status_code == 201, preview.content
    plan_id = preview.json()["data"]["id"]
    apply_url = f"/api/cpa/quota-adjustments/{plan_id}/apply"
    assert client.post(apply_url, **headers).status_code == 200
    adjustment_id = client.post(apply_url, **headers).json()["data"]["id"]
    reverse_url = f"/api/cpa/quota-adjustments/{adjustment_id}/reverse"
    assert (
        client.post(
            reverse_url,
            {"reason": "复核后撤销"},
            content_type="application/json",
            **headers,
        ).status_code
        == 200
    )
