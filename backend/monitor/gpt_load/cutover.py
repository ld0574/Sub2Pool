"""Move one subscription account from CPA to GPT-Load without splitting facts."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone
from rest_framework.serializers import ValidationError

from ..cpa.participants import record_contract
from ..cpa.usage import _refresh_cpa_account_history
from ..history_state import fenced_fact_write
from ..models import (
    APIUsageRequestFact,
    AppSettings,
    CPAAccountCollectionInterval,
    CPAClaimPlan,
    HistoricalRebuildRun,
    HistoryMaintenanceState,
    MonitoredAccount,
    Observation,
    ParticipantAPIUsageSnapshot,
    ParticipantBalanceOperationSource,
    ParticipantUsageSample,
    QuotaPool,
    ResearchEvidenceBatch,
    Sub2APIUserUsageSample,
    UsageSamplePoint,
)
from ..replay import rebuild_account


@dataclass(frozen=True)
class CutoverResult:
    account: MonitoredAccount
    absorbed_account_id: int | None = None


def _latest(first, second):
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _merge_blockers(
    source: MonitoredAccount,
    target: MonitoredAccount,
) -> list[str]:
    """Return facts that cannot be safely reinterpreted as the target account."""

    source_fact_key = source.fact_key
    blockers: list[str] = []
    if source.pool_id != target.pool_id and source.pool.allocations.exists():
        blockers.append("误建账号已经单独配置了成员份额")
    if source.cpa_owner_bindings.exists():
        blockers.append("误建账号已经配置了车主归属")
    if source.cpa_reset_requests.exists():
        blockers.append("误建账号已经提交过额度重置")
    if CPAClaimPlan.objects.filter(account=source).exists():
        blockers.append("误建账号存在请求认领计划")
    if source.quota_adjustment_plans.exists() or source.quota_adjustments.exists():
        blockers.append("误建账号存在人工额度归因记录")
    if ParticipantBalanceOperationSource.objects.filter(account=source).exists():
        blockers.append("误建账号参与过余额写入操作")
    if Sub2APIUserUsageSample.objects.filter(account_id=source_fact_key).exists():
        blockers.append("误建账号存在不兼容的 Sub2API 用户事实")
    if APIUsageRequestFact.objects.filter(account_id=source_fact_key).exists():
        blockers.append("误建账号存在不兼容的 API 用量事实")
    if ResearchEvidenceBatch.objects.filter(account_id=source_fact_key).exists():
        blockers.append("误建账号已经生成研究证据")

    source_point_times = UsageSamplePoint.objects.filter(
        account_id=source_fact_key
    ).values_list("observed_at", flat=True)
    if UsageSamplePoint.objects.filter(
        account_id=target.fact_key,
        observed_at__in=source_point_times,
    ).exists():
        blockers.append("两个本地账号存在同一时刻的采样事实")
    return blockers


def _absorb_independent_account(
    *,
    source: MonitoredAccount,
    target: MonitoredAccount,
) -> None:
    """Absorb an accidentally-created GPT-Load account into the CPA identity."""

    blockers = _merge_blockers(source, target)
    if blockers:
        raise ValidationError(
            "无法自动并回误建账号：" + "；".join(blockers)
        )

    source_fact_key = source.fact_key
    target_fact_key = target.fact_key
    source_pool_id = source.pool_id

    target.authorized_users.add(*source.authorized_users.all())
    source.memberships.all().delete()
    # These are reproducible projections. They must be regenerated against the
    # preserved CPA pool instead of retaining the accidental independent pool.
    ParticipantUsageSample.objects.filter(account_id=source_fact_key).delete()
    ParticipantAPIUsageSnapshot.objects.filter(account_id=source_fact_key).delete()
    HistoricalRebuildRun.objects.filter(account_id=source_fact_key).delete()
    source.cpa_contracts.all().delete()

    source.cpa_usage_events.update(account=target)
    UsageSamplePoint.objects.filter(account_id=source_fact_key).update(
        account_id=target_fact_key
    )
    Observation.objects.filter(account_id=source_fact_key).update(
        account_id=target_fact_key
    )
    source.cpa_collection_intervals.update(account=target)

    source.delete()
    if source_pool_id != target.pool_id:
        QuotaPool.objects.filter(
            pk=source_pool_id,
            accounts__isnull=True,
            allocations__isnull=True,
        ).delete()


def cutover_cpa_account(
    *,
    config: AppSettings,
    account_id: int,
    group_id: int,
    credential_id: int,
    name: str = "",
) -> CutoverResult:
    """Keep one account/pool while changing its active request-log source."""

    account = MonitoredAccount.objects.filter(pk=account_id).first()
    if account is None:
        raise ValidationError("监控账号不存在")
    if account.provider != "cpa":
        raise ValidationError("只有 CPA 账号可以原地续接到 GPT-Load")

    duplicate = (
        MonitoredAccount.objects.filter(gpt_load_credential_id=credential_id)
        .exclude(pk=account.pk)
        .first()
    )
    fact_keys = [account.fact_key]
    if duplicate is not None:
        fact_keys.append(duplicate.fact_key)

    absorbed_account_id: int | None = None
    absorbed_fact_key: int | None = None
    with fenced_fact_write(fact_keys) as guards:
        with transaction.atomic():
            account = MonitoredAccount.objects.select_for_update().get(pk=account_id)
            if account.provider != "cpa":
                raise ValidationError("只有 CPA 账号可以原地续接到 GPT-Load")

            current_duplicate = (
                MonitoredAccount.objects.select_for_update()
                .filter(gpt_load_credential_id=credential_id)
                .exclude(pk=account.pk)
                .first()
            )
            if (current_duplicate is None) != (duplicate is None) or (
                current_duplicate is not None
                and duplicate is not None
                and current_duplicate.pk != duplicate.pk
            ):
                raise ValidationError("账号状态刚刚发生变化，请刷新后重试")

            cutover_at = timezone.now()
            synced_through = None
            if current_duplicate is not None:
                if (
                    current_duplicate.provider != "gpt_load"
                    or current_duplicate.cpa_auth_index is not None
                    or current_duplicate.gpt_load_group_id != group_id
                ):
                    raise ValidationError(
                        "该 GPT-Load Credential 已被其他账号使用"
                    )
                absorbed_account_id = current_duplicate.pk
                absorbed_fact_key = current_duplicate.fact_key
                cutover_at = current_duplicate.gpt_load_cutover_at
                if cutover_at is None:
                    raise ValidationError("误建 GPT-Load 账号缺少切换时间")
                synced_through = current_duplicate.gpt_load_logs_synced_through
                account.last_local_check_at = _latest(
                    account.last_local_check_at,
                    current_duplicate.last_local_check_at,
                )
                account.last_upstream_check_at = _latest(
                    account.last_upstream_check_at,
                    current_duplicate.last_upstream_check_at,
                )
                account.last_success_at = _latest(
                    account.last_success_at,
                    current_duplicate.last_success_at,
                )
                account.next_local_check_at = current_duplicate.next_local_check_at
                account.last_error = current_duplicate.last_error
            open_interval = (
                account.cpa_collection_intervals.select_for_update()
                .filter(disconnected_at__isnull=True)
                .first()
            )
            if open_interval is not None:
                open_interval.disconnected_at = max(
                    cutover_at,
                    open_interval.connected_at,
                )
                open_interval.end_reliable = True
                open_interval.save(
                    update_fields=[
                        "disconnected_at",
                        "end_reliable",
                        "updated_at",
                    ]
                )

            if current_duplicate is not None:
                _absorb_independent_account(
                    source=current_duplicate,
                    target=account,
                )

            account.provider = "gpt_load"
            account.gpt_load_group_id = group_id
            account.gpt_load_credential_id = credential_id
            account.gpt_load_cutover_at = cutover_at
            account.gpt_load_logs_synced_through = synced_through
            account.name = name or account.name
            account.quota_query_mode = "direct"
            account.save(
                update_fields=[
                    "provider",
                    "gpt_load_group_id",
                    "gpt_load_credential_id",
                    "gpt_load_cutover_at",
                    "gpt_load_logs_synced_through",
                    "name",
                    "quota_query_mode",
                    "last_local_check_at",
                    "last_upstream_check_at",
                    "last_success_at",
                    "next_local_check_at",
                    "last_error",
                    "updated_at",
                ]
            )
            if not account.cpa_collection_intervals.filter(
                session_key=f"gpt-load-{group_id}-{credential_id}",
            ).exists():
                CPAAccountCollectionInterval.objects.create(
                    account=account,
                    session_key=f"gpt-load-{group_id}-{credential_id}",
                    connected_at=cutover_at,
                    end_reliable=True,
                )
            record_contract(account, cutover_at)

            if absorbed_account_id is not None:
                _refresh_cpa_account_history(config, account)
                rebuild_account(
                    account.fact_key,
                    config,
                    guard=guards[account.fact_key],
                )

    if absorbed_fact_key is not None:
        HistoryMaintenanceState.objects.filter(account_id=absorbed_fact_key).delete()
    account.refresh_from_db()
    return CutoverResult(
        account=account,
        absorbed_account_id=absorbed_account_id,
    )
