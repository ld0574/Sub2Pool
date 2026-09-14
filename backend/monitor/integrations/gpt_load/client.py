"""Authenticated client for the GPT-Load management API."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator
from urllib.parse import urljoin

import httpx

from ...models import AppSettings
from ...secrets import decrypt_secret
from ..sub2api.dto import WeeklyWindow


class GPTLoadError(RuntimeError):
    """A user-displayable error that never contains the GPT-Load auth key."""


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}") from exc
    if parsed <= 0:
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}")
    return parsed


def _nonnegative_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}") from exc
    if parsed < 0:
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}")
    return parsed


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}") from exc
    if not parsed.is_finite():
        raise GPTLoadError(f"GPT-Load 返回了无效字段 {field}")
    return parsed


class GPTLoadClient:
    def __init__(
        self,
        config: AppSettings,
        *,
        base_url: str | None = None,
        auth_key: str | None = None,
        request_timeout_seconds: int | None = None,
        verify_tls: bool | None = None,
    ):
        key = auth_key or decrypt_secret(config.gpt_load_auth_key_encrypted)
        if not key:
            raise GPTLoadError("尚未配置 GPT-Load AUTH_KEY")
        self.base_url = (base_url or config.gpt_load_base_url).strip().rstrip("/") + "/"
        self.client = httpx.Client(
            headers={
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
            timeout=request_timeout_seconds or config.request_timeout_seconds,
            verify=config.verify_tls if verify_tls is None else verify_tls,
            follow_redirects=False,
        )

    def __enter__(self) -> "GPTLoadClient":
        return self

    def __exit__(self, *_args) -> None:
        self.client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = urljoin(self.base_url, f"api/{path.lstrip('/')}")
        try:
            response = self.client.request(
                method,
                url,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise GPTLoadError(
                f"无法连接 GPT-Load：{exc.__class__.__name__}"
            ) from exc
        try:
            envelope = response.json()
        except ValueError as exc:
            raise GPTLoadError("GPT-Load 返回的不是 JSON") from exc
        if not isinstance(envelope, dict):
            raise GPTLoadError("GPT-Load 响应结构错误")
        code = envelope.get("code")
        if response.status_code >= 400 or code not in (0, "0"):
            message = str(envelope.get("message") or "").strip()
            suffix = f"：{message}" if message else ""
            raise GPTLoadError(
                f"GPT-Load 返回 HTTP {response.status_code}{suffix}"
            )
        if "data" not in envelope:
            raise GPTLoadError("GPT-Load 响应缺少 data")
        return envelope["data"]

    def test_connection(self) -> dict[str, Any]:
        data = self._request("GET", "home")
        if not isinstance(data, dict) or not isinstance(data.get("inventory"), dict):
            raise GPTLoadError("GPT-Load home 响应结构错误")
        return {
            "connected": True,
            "version": str(data.get("version") or ""),
            "inventory": data["inventory"],
        }

    def _paged_items(self, path: str, *, page_size: int = 100) -> Iterator[dict]:
        page = 1
        while True:
            data = self._request(
                "GET",
                path,
                params={"page": page, "page_size": page_size},
            )
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise GPTLoadError(f"GPT-Load {path} 响应结构错误")
            for item in data["items"]:
                if isinstance(item, dict):
                    yield item
            pagination = data.get("pagination")
            if not isinstance(pagination, dict):
                raise GPTLoadError(f"GPT-Load {path} 分页结构错误")
            total_pages = _nonnegative_int(
                pagination.get("total_pages", 0),
                f"{path}.pagination.total_pages",
            )
            if page >= total_pages:
                return
            page += 1

    def list_source_accounts(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        groups = list(self._paged_items("groups"))
        for group in groups:
            if str(group.get("connection_type") or "") != "subscription":
                continue
            group_id = _positive_int(group.get("id"), "groups.id")
            for credential in self._paged_items(f"groups/{group_id}/credentials"):
                credential_id = _positive_int(
                    credential.get("credential_id"),
                    "credentials.credential_id",
                )
                account = credential.get("account")
                if not isinstance(account, dict):
                    account = {}
                observation = credential.get("observation")
                plan = {}
                if isinstance(observation, dict):
                    snapshot = observation.get("snapshot")
                    if isinstance(snapshot, dict) and isinstance(
                        snapshot.get("plan_summary"), dict
                    ):
                        plan = snapshot["plan_summary"]
                result.append(
                    {
                        "group_id": group_id,
                        "group_name": str(group.get("name") or f"Group {group_id}"),
                        "channel_id": str(group.get("channel_id") or ""),
                        "credential_id": credential_id,
                        "email": str(
                            account.get("email") or account.get("email_mask") or ""
                        ),
                        "mask": str(credential.get("mask") or ""),
                        "plan_type": str(plan.get("name") or ""),
                        "configured_status": str(
                            credential.get("configured_status") or ""
                        ),
                        "effective_status": str(
                            credential.get("effective_status") or ""
                        ),
                    }
                )
        return result

    def list_access_keys(self) -> list[dict[str, Any]]:
        return list(self._paged_items("access-keys"))

    def iter_logs(
        self,
        *,
        group_id: int,
        credential_id: int,
        from_ms: int,
        to_ms: int,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {
                "group_id": group_id,
                "credential_id": credential_id,
                "from_ms": from_ms,
                "to_ms": to_ms,
                "limit": 200,
            }
            if cursor:
                params["cursor"] = cursor
            data = self._request("GET", "logs", params=params)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise GPTLoadError("GPT-Load logs 响应结构错误")
            for item in data["items"]:
                if not isinstance(item, dict):
                    raise GPTLoadError("GPT-Load logs 包含无效记录")
                yield item
            next_cursor = data.get("next_cursor")
            if next_cursor is None:
                return
            if not isinstance(next_cursor, str) or not next_cursor:
                raise GPTLoadError("GPT-Load logs 返回了无效游标")
            cursor = next_cursor

    def query_weekly_window(self, group_id: int, credential_id: int) -> WeeklyWindow:
        data = self._request(
            "POST",
            f"groups/{group_id}/credentials/{credential_id}/observation-refresh",
            json_body={},
        )
        if not isinstance(data, dict) or data.get("state") != "fresh":
            raise GPTLoadError("GPT-Load 订阅账号额度观测不可用")
        snapshot = data.get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("quota_windows"), list
        ):
            raise GPTLoadError("GPT-Load 额度观测结构错误")
        candidates = [
            row
            for row in snapshot["quota_windows"]
            if isinstance(row, dict)
            and row.get("scope") == "account"
            and row.get("window_seconds") is not None
            and row.get("reset_at_ms") is not None
        ]
        if not candidates:
            raise GPTLoadError("GPT-Load 订阅账号没有账号级周期额度")
        weekly = min(
            candidates,
            key=lambda row: abs(int(row["window_seconds"]) - 604800),
        )
        seconds = _positive_int(weekly.get("window_seconds"), "window_seconds")
        if abs(seconds - 604800) > 86400:
            raise GPTLoadError(f"未找到七天窗口，最接近的窗口为 {seconds} 秒")
        reset_at_ms = _positive_int(weekly.get("reset_at_ms"), "reset_at_ms")
        used = weekly.get("used")
        used_percent = (
            _decimal(used, "used")
            if used is not None
            else _decimal(weekly.get("utilization"), "utilization") * Decimal("100")
        )
        observed_at_ms = _positive_int(data.get("observed_at_ms"), "observed_at_ms")
        observed_at = datetime.fromtimestamp(
            observed_at_ms / 1000,
            tz=timezone.utc,
        )
        reset_at = reset_at_ms // 1000
        plan = snapshot.get("plan_summary")
        if not isinstance(plan, dict):
            plan = {}
        return WeeklyWindow(
            used_percent=used_percent,
            window_seconds=seconds,
            reset_after_seconds=max(0, reset_at - int(observed_at.timestamp())),
            reset_at=reset_at,
            slot=str(weekly.get("id") or "weekly"),
            sampled_at=observed_at.isoformat(),
            plan_type=str(plan.get("name") or "") or None,
        )
