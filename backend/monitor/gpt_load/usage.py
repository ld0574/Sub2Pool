"""Import immutable GPT-Load request logs into the existing usage ledger."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from ..cpa.usage import _refresh_cpa_account_history
from ..history_state import LeaseGuard
from ..integrations.gpt_load import GPTLoadClient, GPTLoadError
from ..models import (
    AppSettings,
    CPAAPIKey,
    CPAUsageEvent,
    HistoryMaintenanceState,
    MonitoredAccount,
    Observation,
)
from ..replay import rebuild_account


def access_key_hash(external_key_id: int) -> str:
    """Stable, non-secret identity shared by imported logs and key bindings."""

    return hashlib.sha256(
        f"gpt_load:access_key:{external_key_id}".encode("utf-8")
    ).hexdigest()


def event_fingerprint(request_id: str) -> str:
    return hashlib.sha256(f"gpt_load:{request_id}".encode("utf-8")).hexdigest()


def _integer(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise GPTLoadError(f"GPT-Load 日志字段 {field} 无效") from exc
    if result < 0:
        raise GPTLoadError(f"GPT-Load 日志字段 {field} 无效")
    return result


def _event_time(value: Any) -> datetime:
    milliseconds = _integer(value, "completed_at_ms")
    try:
        return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise GPTLoadError("GPT-Load 日志 completed_at_ms 无效") from exc


def sync_access_keys(
    client: GPTLoadClient,
    rows: Iterable[dict[str, Any]] | None = None,
) -> dict[int, CPAAPIKey]:
    result: dict[int, CPAAPIKey] = {}
    for row in rows if rows is not None else client.list_access_keys():
        external_id = _integer(row.get("id"), "access_key.id")
        if external_id <= 0:
            raise GPTLoadError("GPT-Load 日志 access_key.id 无效")
        masked = str(row.get("masked_key") or "")
        defaults = {
            "key_hash": access_key_hash(external_id),
            "name": str(row.get("name") or "")[:80],
        }
        if len(masked) >= 4:
            defaults["hint"] = masked[-4:]
        key, _created = CPAAPIKey.objects.update_or_create(
            source="gpt_load",
            external_key_id=external_id,
            defaults=defaults,
        )
        result[external_id] = key
    return result


def _prepare_event(
    account: MonitoredAccount,
    payload: dict[str, Any],
    keys: dict[int, CPAAPIKey],
) -> CPAUsageEvent:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise GPTLoadError("GPT-Load 日志缺少 request_id")
    occurred_at = _event_time(payload.get("completed_at_ms"))
    if account.gpt_load_cutover_at is None or occurred_at < account.gpt_load_cutover_at:
        raise GPTLoadError("GPT-Load 返回了切换时间之前的日志")
    access_key = payload.get("access_key")
    if not isinstance(access_key, dict):
        raise GPTLoadError("GPT-Load 日志缺少 access_key")
    external_key_id = _integer(access_key.get("id"), "access_key.id")
    key = keys.get(external_key_id)
    if key is None:
        key, _created = CPAAPIKey.objects.get_or_create(
            source="gpt_load",
            external_key_id=external_key_id,
            defaults={
                "key_hash": access_key_hash(external_key_id),
                "hint": "",
                "name": str(access_key.get("name") or "")[:80],
            },
        )
        keys[external_key_id] = key
    input_tokens = _integer(payload.get("input_tokens", 0), "input_tokens")
    cached_tokens = _integer(
        payload.get("cache_read_tokens", 0),
        "cache_read_tokens",
    )
    output_tokens = _integer(payload.get("output_tokens", 0), "output_tokens")
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, dict):
        reasoning = {}
    return CPAUsageEvent(
        account=account,
        event_fingerprint=event_fingerprint(request_id),
        request_id=request_id[:255],
        source="gpt_load",
        source_cost_nano_usd=_integer(
            payload.get("estimated_cost_nano_usd", 0),
            "estimated_cost_nano_usd",
        ),
        usage_state=str(payload.get("usage_state") or "")[:32],
        cost_state=str(payload.get("cost_state") or "")[:32],
        pricing_completeness=str(
            payload.get("pricing_completeness") or ""
        )[:32],
        occurred_at=occurred_at,
        model=str(
            payload.get("client_model")
            or payload.get("upstream_model")
            or "unknown"
        )[:255],
        alias=str(payload.get("credential_name") or "")[:255],
        endpoint=str(
            payload.get("operation") or payload.get("protocol") or ""
        )[:255],
        provider=str(payload.get("upstream_protocol") or "gpt_load")[:64],
        api_key_hash=key.key_hash,
        api_key_hint=key.hint,
        input_tokens=input_tokens,
        cached_input_tokens=cached_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=0,
        reasoning_effort=str(reasoning.get("effort") or "")[:32],
        total_tokens=input_tokens + output_tokens,
        failed=str(payload.get("status") or "") != "success",
        latency_ms=_integer(payload.get("duration_ms", 0), "duration_ms"),
        ttft_ms=_integer(payload.get("first_response_ms") or 0, "first_response_ms"),
        requested_service_tier=str(payload.get("pricing_mode") or "")[:32],
        response_service_tier="",
    )


def sync_usage(
    config: AppSettings,
    account: MonitoredAccount,
    client: GPTLoadClient,
    guard: LeaseGuard,
    *,
    through: datetime | None = None,
) -> dict[str, int]:
    if account.provider != "gpt_load" or account.gpt_load_cutover_at is None:
        raise GPTLoadError("账号尚未切换到 GPT-Load")
    through = (through or timezone.now()).astimezone(UTC)
    overlap_start = (
        account.gpt_load_logs_synced_through - timedelta(minutes=5)
        if account.gpt_load_logs_synced_through
        else account.gpt_load_cutover_at
    )
    started_at = max(account.gpt_load_cutover_at, overlap_start).astimezone(UTC)
    if through <= started_at:
        return {"created": 0, "duplicates": 0}

    keys = sync_access_keys(client)
    candidates = [
        _prepare_event(account, row, keys)
        for row in client.iter_logs(
            group_id=account.gpt_load_group_id,
            credential_id=account.gpt_load_credential_id,
            from_ms=int(started_at.timestamp() * 1000),
            to_ms=int(through.timestamp() * 1000),
        )
        if _event_time(row.get("completed_at_ms")) >= account.gpt_load_cutover_at
    ]
    by_fingerprint = {event.event_fingerprint: event for event in candidates}
    existing = set(
        CPAUsageEvent.objects.filter(
            event_fingerprint__in=by_fingerprint
        ).values_list("event_fingerprint", flat=True)
    )
    to_create = [
        event
        for fingerprint, event in by_fingerprint.items()
        if fingerprint not in existing
    ]

    guard.renew()
    with transaction.atomic():
        state = HistoryMaintenanceState.objects.select_for_update().get(
            account_id=account.fact_key
        )
        guard.assert_owned(state)
        CPAUsageEvent.objects.bulk_create(
            to_create,
            batch_size=500,
            ignore_conflicts=True,
        )
        MonitoredAccount.objects.filter(pk=account.pk).update(
            gpt_load_logs_synced_through=through
        )
        account.gpt_load_logs_synced_through = through
        if to_create:
            state.fact_revision += 1
            state.save(update_fields=["fact_revision", "updated_at"])

    if to_create:
        earliest = min(event.occurred_at for event in to_create)
        if Observation.objects.filter(
            account_id=account.fact_key,
            observed_at__gte=earliest,
        ).exists():
            _refresh_cpa_account_history(
                config,
                account,
                observed_from=earliest,
            )
            rebuild_account(account.fact_key, config, guard=guard)
    return {
        "created": len(to_create),
        "duplicates": len(candidates) - len(to_create),
    }
