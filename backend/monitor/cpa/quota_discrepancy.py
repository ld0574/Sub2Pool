"""Evidence-bound GPT-Load quota discrepancies and manual attributions."""

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ..accounting.boundaries import same_official_reset
from ..fact_utils import canonical_digest
from ..history_state import fenced_fact_write
from ..models import (
    AppSettings,
    CPAQuotaAdjustment,
    CPAQuotaAdjustmentPlan,
    CPAUsageEvent,
    Observation,
)
from .capacity_estimate import particle_capacity_estimate
from .participants import coverage_data
from .usage import cpa_event_cost

ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY = Decimal("0.000001")


@dataclass(frozen=True)
class QuotaDiscrepancy:
    account_id: int
    cycle_started_at: str
    cycle_ended_at: str
    baseline_observation_id: int
    capacity_observation_id: int | None
    latest_observation_id: int
    baseline_observed_at: str
    observed_through: str
    official_delta_percent: float
    request_usage_usd: float
    suggested_usd: float
    lower_usd: float
    upper_usd: float
    manual_adjustment_usd: float
    remaining_suggested_usd: float
    remaining_lower_usd: float
    remaining_upper_usd: float
    held_unexplained_usd: float
    materiality_usd: float
    can_attribute: bool
    status: str
    reasons: list[str]
    over_attributed: bool
    member_adjustments: dict[int, float]
    member_holds: dict[int, float]
    unallocated_hold_usd: float
    eligible_participant_ids: list[int]


def _cycle_groups(account, now):
    groups = []
    observations = (
        Observation.objects.filter(
            account_id=account.fact_key,
            excluded_at__isnull=True,
            observed_at__lte=now,
            upstream_resets_at__isnull=False,
            window_seconds__gt=0,
        )
        .exclude(exclusion_source="manual")
        .order_by("observed_at", "id")
    )
    for observation in observations:
        if not groups or not same_official_reset(
            groups[-1][-1].upstream_resets_at,
            observation.upstream_resets_at,
        ):
            groups.append([])
        groups[-1].append(observation)
    return groups


def _contract_at(account, at):
    return account.cpa_contracts.filter(effective_at__lte=at).order_by(
        "-effective_at", "-id"
    ).first()


def _eligible_participants(account, start, end):
    ids = set(account.pool.allocations.values_list("participant_id", flat=True))
    active_at_start = _contract_at(account, start)
    if active_at_start is not None:
        ids.update(
            int(row["participant_id"]) for row in active_at_start.allocations
        )
    for contract in account.cpa_contracts.filter(
        effective_at__gte=start,
        effective_at__lt=end,
    ).order_by("effective_at", "id"):
        ids.update(int(row["participant_id"]) for row in contract.allocations)
    return sorted(ids)


def _calibrated_capacity(account, baseline):
    for observation in Observation.objects.filter(
        account_id=account.fact_key,
        observed_at__lt=baseline.observed_at,
        excluded_at__isnull=True,
        valid_sample=True,
    ).order_by("-observed_at", "-id"):
        estimate = particle_capacity_estimate(observation)
        if (
            estimate
            and not estimate["prior_only"]
            and estimate["lower_usd"] is not None
            and estimate["upper_usd"] is not None
        ):
            return observation, estimate
    return None, None


def _adjustments(account, start, end):
    rows = list(
        account.quota_adjustments.filter(
            cycle_ended_at__gte=end - timedelta(minutes=10),
            cycle_ended_at__lte=end + timedelta(minutes=10),
        ).select_related("participant", "created_by", "reversal_of")
    )
    rows = [row for row in rows if same_official_reset(row.cycle_ended_at, end)]
    total = sum((row.amount_usd for row in rows), ZERO)
    by_member = defaultdict(lambda: ZERO)
    for row in rows:
        by_member[row.participant_id] += row.amount_usd
    return total, by_member


def _request_cost(account, config, baseline, latest):
    total = ZERO
    unknown = 0
    events = CPAUsageEvent.objects.filter(
        account=account,
        source="gpt_load",
        occurred_at__gt=baseline.observed_at,
        occurred_at__lte=latest.observed_at,
    ).order_by("occurred_at", "id")
    for event in events:
        cost, is_unknown = cpa_event_cost(event, config)
        total += cost
        unknown += int(is_unknown)
    return total, unknown


def _discrepancy_for_group(account, group, config):
    gpt_observations = [
        row
        for row in group
        if row.observed_at >= account.gpt_load_cutover_at
        and isinstance(row.raw_window, dict)
        and row.raw_window.get("provider") == "gpt_load"
    ]
    if not gpt_observations:
        return None
    baseline, latest = gpt_observations[0], gpt_observations[-1]
    start = latest.upstream_resets_at - timedelta(seconds=latest.window_seconds)
    end = latest.upstream_resets_at
    reasons = []
    capacity_observation, capacity = _calibrated_capacity(account, baseline)
    if capacity is None:
        reasons.append("基线前没有已校准容量区间")
    if (
        account.gpt_load_logs_synced_through is None
        or account.gpt_load_logs_synced_through < latest.observed_at
    ):
        reasons.append("GPT-Load 请求日志尚未同步到额度观测")
    if not coverage_data(account, baseline.observed_at, latest.observed_at)["complete"]:
        reasons.append("证据窗口采集覆盖不完整")
    request_cost, unknown = _request_cost(account, config, baseline, latest)
    if unknown:
        reasons.append(f"证据窗口有 {unknown} 条请求缺少模型价格")
    delta = max(ZERO, latest.upstream_used_percent - baseline.upstream_used_percent)
    if delta <= ZERO:
        reasons.append("基线后尚无官方额度增量")
    adjusted, by_member = _adjustments(account, start, end)
    point = lower = upper = threshold = ZERO
    if capacity is not None and delta > ZERO:
        point_capacity = Decimal(str(capacity["capacity_usd"]))
        lower_capacity = Decimal(str(capacity["lower_usd"]))
        upper_capacity = Decimal(str(capacity["upper_usd"]))
        point = max(ZERO, point_capacity * delta / HUNDRED - request_cost)
        lower = max(ZERO, lower_capacity * delta / HUNDRED - request_cost)
        upper = max(ZERO, upper_capacity * delta / HUNDRED - request_cost)
        threshold = max(Decimal("1"), lower_capacity * Decimal("0.001"))
        if lower <= threshold:
            reasons.append("差额下限未超过可靠性阈值")
    positive_adjusted = max(ZERO, adjusted)
    remaining_point = max(ZERO, point - positive_adjusted)
    remaining_lower = max(ZERO, lower - positive_adjusted)
    remaining_upper = max(ZERO, upper - positive_adjusted)
    material = lower > threshold and not any(
        reason
        for reason in reasons
        if reason not in {"差额下限未超过可靠性阈值"}
    )
    held = remaining_lower if material else ZERO
    contract = _contract_at(account, latest.observed_at)
    member_holds = defaultdict(lambda: ZERO)
    allocated_hold = ZERO
    if contract and held > ZERO:
        for allocation in contract.allocations:
            participant_id = int(allocation["participant_id"])
            amount = held * Decimal(str(allocation["share_percent"])) / HUNDRED
            member_holds[participant_id] += amount
            allocated_hold += amount
    eligible = _eligible_participants(account, start, end)
    can_attribute = material and remaining_upper > ZERO and bool(eligible)
    over_attributed = adjusted > upper and adjusted > ZERO
    if over_attributed:
        status = "over_attributed"
    elif can_attribute:
        status = "ready"
    elif positive_adjusted and remaining_upper <= ZERO:
        status = "explained"
    else:
        status = "insufficient_evidence"
    return QuotaDiscrepancy(
        account_id=account.id,
        cycle_started_at=start.isoformat(),
        cycle_ended_at=end.isoformat(),
        baseline_observation_id=baseline.id,
        capacity_observation_id=capacity_observation.id if capacity_observation else None,
        latest_observation_id=latest.id,
        baseline_observed_at=baseline.observed_at.isoformat(),
        observed_through=latest.observed_at.isoformat(),
        official_delta_percent=float(delta),
        request_usage_usd=float(request_cost),
        suggested_usd=float(point.quantize(MONEY)),
        lower_usd=float(lower.quantize(MONEY)),
        upper_usd=float(upper.quantize(MONEY)),
        manual_adjustment_usd=float(adjusted.quantize(MONEY)),
        remaining_suggested_usd=float(remaining_point.quantize(MONEY)),
        remaining_lower_usd=float(remaining_lower.quantize(MONEY)),
        remaining_upper_usd=float(remaining_upper.quantize(MONEY)),
        held_unexplained_usd=float(held.quantize(MONEY)),
        materiality_usd=float(threshold.quantize(MONEY)),
        can_attribute=can_attribute,
        status=status,
        reasons=reasons,
        over_attributed=over_attributed,
        member_adjustments={
            pk: float(value.quantize(MONEY)) for pk, value in by_member.items()
        },
        member_holds={
            pk: float(value.quantize(MONEY)) for pk, value in member_holds.items()
        },
        unallocated_hold_usd=float(max(ZERO, held - allocated_hold).quantize(MONEY)),
        eligible_participant_ids=eligible,
    )


def quota_discrepancies(account, config=None, now=None):
    if account.provider != "gpt_load" or account.gpt_load_cutover_at is None:
        return []
    config = config or AppSettings.load()
    now = now or timezone.now()
    return [
        result
        for group in _cycle_groups(account, now)
        if (result := _discrepancy_for_group(account, group, config)) is not None
    ]


def current_quota_discrepancy(account, config=None, now=None):
    now = now or timezone.now()
    return next(
        (
            row
            for row in reversed(quota_discrepancies(account, config, now))
            if datetime.fromisoformat(row.cycle_started_at)
            <= now
            < datetime.fromisoformat(row.cycle_ended_at)
        ),
        None,
    )


def public_discrepancy_data(discrepancy):
    """Expose aggregate evidence without administrator-only audit identifiers."""
    data = asdict(discrepancy)
    for key in (
        "baseline_observation_id",
        "capacity_observation_id",
        "latest_observation_id",
        "member_adjustments",
        "member_holds",
        "eligible_participant_ids",
    ):
        data.pop(key)
    return data


def adjustment_history(account):
    return list(
        account.quota_adjustments.select_related(
            "participant", "created_by", "reversal_of"
        ).order_by("-effective_at", "-created_at")
    )


def _source_digest(account, discrepancy):
    return canonical_digest(
        {
            "account": type(account)
            .objects.filter(pk=account.pk)
            .values(
                "id",
                "provider",
                "pool_id",
                "enabled",
                "gpt_load_group_id",
                "gpt_load_credential_id",
                "gpt_load_cutover_at",
                "gpt_load_logs_synced_through",
            )
            .get(),
            "discrepancy": asdict(discrepancy),
            "observations": list(
                Observation.objects.filter(
                    pk__in=[
                        value
                        for value in (
                            discrepancy.capacity_observation_id,
                            discrepancy.baseline_observation_id,
                            discrepancy.latest_observation_id,
                        )
                        if value is not None
                    ]
                )
                .order_by("id")
                .values()
            ),
            "events": list(
                CPAUsageEvent.objects.filter(
                    account=account,
                    source="gpt_load",
                    occurred_at__gt=discrepancy.baseline_observed_at,
                    occurred_at__lte=discrepancy.observed_through,
                )
                .order_by("id")
                .values()
            ),
            "adjustments": list(account.quota_adjustments.order_by("id").values()),
            "contracts": list(account.cpa_contracts.order_by("id").values()),
        }
    )


def _find_discrepancy(account, latest_observation_id, config=None):
    return next(
        (
            row
            for row in quota_discrepancies(account, config)
            if row.latest_observation_id == latest_observation_id
        ),
        None,
    )


def preview_adjustment(*, account, participant, latest_observation_id, amount_usd, reason, user):
    if account.provider != "gpt_load":
        raise ValidationError("人工额度归因仅适用于 GPT-Load 账号")
    amount = Decimal(str(amount_usd)).quantize(MONEY)
    reason = reason.strip()
    if amount <= ZERO:
        raise ValidationError("确认金额必须大于 0")
    if not reason:
        raise ValidationError("请填写人工归因原因")
    with fenced_fact_write([account.fact_key]):
        discrepancy = _find_discrepancy(account, latest_observation_id)
        if discrepancy is None or not discrepancy.can_attribute:
            raise ValidationError("当前周期没有可人工归因的可靠额度差额")
        if participant.id not in discrepancy.eligible_participant_ids:
            raise ValidationError("只能归因给当前池或该周期历史合同中的成员")
        if amount > Decimal(str(discrepancy.remaining_upper_usd)):
            raise ValidationError("确认金额不能超过当前差额区间上限")
        baseline = Observation.objects.get(pk=discrepancy.baseline_observation_id)
        latest = Observation.objects.get(pk=discrepancy.latest_observation_id)
        preview = {
            **asdict(discrepancy),
            "participant_id": participant.id,
            "participant_name": participant.name,
            "amount_usd": float(amount),
            "reason": reason,
        }
        return CPAQuotaAdjustmentPlan.objects.create(
            account=account,
            participant=participant,
            baseline_observation=baseline,
            latest_observation=latest,
            amount_usd=amount,
            reason=reason,
            source_digest=_source_digest(account, discrepancy),
            preview=preview,
            created_by=user,
            expires_at=timezone.now() + timedelta(minutes=15),
        )


def apply_adjustment(plan_id):
    initial = CPAQuotaAdjustmentPlan.objects.select_related("account").get(pk=plan_id)
    with fenced_fact_write([initial.account.fact_key]):
        with transaction.atomic():
            plan = (
                CPAQuotaAdjustmentPlan.objects.select_for_update()
                .select_related("account", "participant")
                .get(pk=plan_id)
            )
            if plan.applied_at is not None:
                return plan.adjustment
            discrepancy = _find_discrepancy(
                plan.account, plan.latest_observation_id
            )
            if (
                discrepancy is None
                or not discrepancy.can_attribute
                or plan.expires_at <= timezone.now()
                or plan.source_digest != _source_digest(plan.account, discrepancy)
                or plan.amount_usd
                > Decimal(str(discrepancy.remaining_upper_usd))
            ):
                raise ValidationError("归因预览已过期或证据已变化，请重新预览")
            adjustment = CPAQuotaAdjustment.objects.create(
                account=plan.account,
                participant=plan.participant,
                plan=plan,
                baseline_observation=plan.baseline_observation,
                latest_observation=plan.latest_observation,
                cycle_started_at=plan.latest_observation.upstream_resets_at
                - timedelta(seconds=plan.latest_observation.window_seconds),
                cycle_ended_at=plan.latest_observation.upstream_resets_at,
                effective_at=plan.latest_observation.observed_at,
                amount_usd=plan.amount_usd,
                reason=plan.reason,
                evidence=plan.preview,
                created_by=plan.created_by,
            )
            plan.applied_at = timezone.now()
            plan.save(update_fields=["applied_at"])
            return adjustment


def reverse_adjustment(adjustment_id, *, reason, user):
    reason = reason.strip()
    if not reason:
        raise ValidationError("请填写撤销原因")
    initial = CPAQuotaAdjustment.objects.select_related("account").get(pk=adjustment_id)
    with fenced_fact_write([initial.account.fact_key]), transaction.atomic():
        original = (
            CPAQuotaAdjustment.objects.select_for_update()
            .select_related("account", "participant")
            .get(pk=adjustment_id)
        )
        if original.amount_usd <= ZERO or original.reversal_of_id is not None:
            raise ValidationError("只能撤销原始人工归因记录")
        existing = CPAQuotaAdjustment.objects.filter(reversal_of=original).first()
        if existing is not None:
            return existing
        return CPAQuotaAdjustment.objects.create(
            account=original.account,
            participant=original.participant,
            reversal_of=original,
            baseline_observation=original.baseline_observation,
            latest_observation=original.latest_observation,
            cycle_started_at=original.cycle_started_at,
            cycle_ended_at=original.cycle_ended_at,
            effective_at=original.effective_at,
            amount_usd=-original.amount_usd,
            reason=reason,
            evidence={"reversal_of": str(original.id), "original": original.evidence},
            created_by=user,
        )
