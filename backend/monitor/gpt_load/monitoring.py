"""GPT-Load quota capture backed by paginated request-log ingestion."""

from django.utils import timezone

from ..cpa.monitoring import _capture_cpa_window
from ..history_state import LeaseGuard
from ..integrations.gpt_load import GPTLoadClient
from ..models import AppSettings, MonitoredAccount
from .usage import sync_usage


def run_gpt_load_monitor(
    config: AppSettings,
    account: MonitoredAccount,
    source: str,
    guard: LeaseGuard,
) -> dict:
    observed_at = timezone.now()
    with GPTLoadClient(config) as client:
        imported = sync_usage(
            config,
            account,
            client,
            guard,
            through=observed_at,
        )
        window = client.query_weekly_window(
            account.gpt_load_group_id,
            account.gpt_load_credential_id,
        )
    result = _capture_cpa_window(
        config,
        account,
        source,
        guard,
        window=window,
        observed_at=observed_at,
        raw_metadata={
            "provider": "gpt_load",
            "gpt_load_import": imported,
        },
    )
    return {**result, "usage_import": imported}
