"""参与者账号周期用量的时间序列投影。"""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from django.contrib.auth.models import AbstractBaseUser
from django.db import DataError, OperationalError, connection
from django.db.models import Count, F, Min, Sum, Window
from django.db.models.functions import RowNumber, TruncDate, TruncHour

from ..cpa.usage_rollup import EVENT_FIELDS, GPT_COST, GPT_UNPRICED
from ..cpa.usage import cpa_event_cost
from ..models import (
    AppSettings,
    CPAUsageEvent,
    MonitoredAccount,
    Participant,
    ParticipantUsageSample,
)


def participant_usage_series(
    *,
    account: MonitoredAccount,
    user: AbstractBaseUser,
    location: ZoneInfo,
    now: datetime,
    usage_days: int,
    usage_precision: str,
) -> list[dict]:
    """按调用者可见范围和请求粒度生成参与者用量序列。"""
    participants = Participant.objects.filter(sub2api_user_id__isnull=False)
    if not user.is_staff:
        participants = participants.filter(authorized_users=user)

    samples = (
        ParticipantUsageSample.objects.filter(
            observed_at__gte=now - timedelta(days=usage_days),
            account_id=account.fact_key,
            participant__in=participants,
        )
        .select_related("participant")
        .order_by("participant_id", "observed_at", "id")
    )
    usage_buckets: dict[int, dict[str, dict]] = defaultdict(dict)
    for sample in samples:
        local = sample.observed_at.astimezone(location)
        if usage_precision == "raw":
            bucket = sample.observed_at.isoformat()
            label = local.strftime("%m-%d %H:%M")
        elif usage_precision == "hour":
            bucket = local.replace(
                minute=0,
                second=0,
                microsecond=0,
            ).isoformat()
            label = local.strftime("%m-%d %H:00")
        else:
            bucket = local.date().isoformat()
            label = local.strftime("%m-%d")
        # 账号周期用量用于归属权益；用户余额是 Sub2API 的全局可用余额。
        usage_buckets[sample.participant_id][bucket] = {
            "observed_at": sample.observed_at.isoformat(),
            "label": label,
            "account_cycle_usage_usd": float(sample.selected_cost),
            "balance_usd": (
                float(sample.balance_usd) if sample.balance_usd is not None else None
            ),
        }

    return [
        {
            "participant_id": participant.id,
            "participant_name": participant.name,
            "account_id": account.id,
            "external_account_id": account.external_account_id,
            "sub2api_user_id": participant.sub2api_user_id,
            "points": list(usage_buckets[participant.id].values()),
        }
        for participant in participants
    ]


def cpa_api_key_usage_series(
    *,
    config: AppSettings,
    account: MonitoredAccount,
    location: ZoneInfo,
    now: datetime,
    usage_days: int,
    usage_precision: str,
    user=None,
) -> list[dict]:
    events = CPAUsageEvent.objects.filter(
        account=account,
        occurred_at__gte=now - timedelta(days=usage_days),
        occurred_at__lte=now,
    ).order_by("occurred_at", "id")
    if user is not None and not user.is_staff:
        from ..cpa.participants import ownership_filter

        events = events.filter(
            ownership_filter(user.quota_participants.values_list("id", flat=True))
        )
    first_events = (
        events.annotate(
            key_order=Window(
                RowNumber(),
                partition_by=F("api_key_hash"),
                order_by=(F("occurred_at").asc(), F("id").asc()),
            )
        )
        .filter(key_order=1)
        .values("api_key_hash", "api_key_hint")
    )
    hints = {row["api_key_hash"]: row["api_key_hint"] for row in first_events}
    series: dict[str, dict] = {}
    for row in _chart_usage_rows(
        events, config, location, usage_precision, now - timedelta(days=usage_days), now
    ):
        key = row["api_key_hash"] or "unattributed"
        # Logs may add a new key between these read-only queries.
        hint = hints.get(row["api_key_hash"], "")
        item = series.setdefault(
            key,
            {
                "api_key_id": key[:12],
                "api_key_name": (f"API Key ····{hint}" if hint else "未提供 API Key"),
                "total_usage_usd": 0.0,
                "request_count": 0,
                "token_count": 0,
                "unpriced_request_count": 0,
                "cpa_unpriced_request_count": 0,
                "gpt_load_unpriced_request_count": 0,
                "_buckets": {},
            },
        )
        local = row["occurred_at"].astimezone(location)
        if usage_precision == "raw":
            bucket = f"{row['occurred_at'].isoformat()}-{row['id']}"
            label = local.strftime("%m-%d %H:%M:%S")
        elif usage_precision == "hour":
            bucket = local.replace(
                minute=0,
                second=0,
                microsecond=0,
            ).isoformat()
            label = local.strftime("%m-%d %H:00")
        else:
            bucket = local.date().isoformat()
            label = local.strftime("%m-%d")
        point = item["_buckets"].setdefault(
            bucket,
            {
                "observed_at": row["occurred_at"].isoformat(),
                "label": label,
                "usage_usd": 0.0,
                "request_count": 0,
                "token_count": 0,
            },
        )
        cost = float(row["cost"])
        point["usage_usd"] += cost
        point["request_count"] += row["request_count"]
        point["token_count"] += row["total_tokens"]
        item["total_usage_usd"] += cost
        item["request_count"] += row["request_count"]
        item["token_count"] += row["total_tokens"]
        for source in ("cpa", "gpt_load"):
            count = row[f"{source}_unpriced_request_count"]
            item[f"{source}_unpriced_request_count"] += count
            item["unpriced_request_count"] += count
    result = []
    for item in series.values():
        buckets = item.pop("_buckets")
        item["points"] = list(buckets.values())
        result.append(item)
    return sorted(
        result,
        key=lambda item: (-item["total_usage_usd"], item["api_key_name"]),
    )


def _has_offset_change(location, start, end):
    """Do not collapse DST's two local hours into one SQL truncated bucket."""
    cursor = start.astimezone(UTC)
    offset = cursor.astimezone(location).utcoffset()
    end = end.astimezone(UTC)
    while cursor < end:
        cursor = min(cursor + timedelta(hours=1), end)
        if cursor.astimezone(location).utcoffset() != offset:
            return True
    return False


def _stream_chart_rows(events, config):
    for data in events.values("id", "api_key_hash", *EVENT_FIELDS).iterator(
        chunk_size=2000
    ):
        cost, unknown = cpa_event_cost(SimpleNamespace(**data), config)
        yield {
            "id": data["id"],
            "api_key_hash": data["api_key_hash"],
            "occurred_at": data["occurred_at"],
            "cost": cost,
            "request_count": 1,
            "total_tokens": data["total_tokens"],
            "cpa_unpriced_request_count": int(unknown and data["source"] != "gpt_load"),
            "gpt_load_unpriced_request_count": int(
                unknown and data["source"] == "gpt_load"
            ),
        }


def _chart_usage_rows(events, config, location, precision, start, end):
    if precision == "raw" or (
        precision == "hour" and _has_offset_change(location, start, end)
    ):
        yield from _stream_chart_rows(events, config)
        return
    bucket = (
        TruncHour("occurred_at", tzinfo=location)
        if precision == "hour"
        else TruncDate("occurred_at", tzinfo=location)
    )
    grouped = (
        events.filter(source="gpt_load")
        .order_by()
        .annotate(chart_bucket=bucket)
        .values(
            "api_key_hash",
            "chart_bucket",
        )
        .annotate(
            occurred_at=Min("occurred_at"),
            request_count=Count("id"),
            total_tokens=Sum("total_tokens"),
            cost_nano=Sum("source_cost_nano_usd", filter=GPT_COST),
            gpt_load_unpriced_request_count=Count("id", filter=GPT_UNPRICED),
        )
    )
    try:
        rows = list(grouped)
    except (OperationalError, DataError) as exc:
        if connection.vendor != "sqlite" or "integer overflow" not in str(exc).lower():
            raise
        yield from _stream_chart_rows(events, config)
        return
    for row in rows:
        row["cost"] = Decimal(row.pop("cost_nano") or 0) / Decimal("1000000000")
        row["cpa_unpriced_request_count"] = 0
    rows.extend(_stream_chart_rows(events.exclude(source="gpt_load"), config))
    # Bucket timestamps and labels must still come from the first real request,
    # not the truncated boundary; merge CPA/GPT rows in chronological order.
    yield from sorted(rows, key=lambda row: (row["occurred_at"], row.get("id", 0)))
