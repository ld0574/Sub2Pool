"""Request-local, read-only inputs shared by dashboard accounting projections.

Never cache across HTTP requests: late logs, claims, prices, exclusions and manual
adjustments must be visible on the next refresh, including for another user.
"""

from bisect import bisect_left, bisect_right
from decimal import Decimal
from types import SimpleNamespace

from django.db.models import F

from ..models import CPAUsageEvent, Observation
from .participants import coverage_data
from .usage import cpa_event_cost

ZERO = Decimal("0")
EVENT_FIELDS = (
    "id",
    "account_id",
    "occurred_at",
    "api_key_hash",
    "total_tokens",
    "source",
    "source_cost_nano_usd",
    "usage_state",
    "cost_state",
    "pricing_completeness",
    "model",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "requested_service_tier",
    "response_service_tier",
)
OBSERVATION_FIELDS = (
    "id",
    "account_id",
    "observed_at",
    "upstream_resets_at",
    "window_seconds",
    "excluded_at",
    "exclusion_source",
    "attribution_started_at",
    "valid_sample",
    "upstream_used_percent",
    "interval_used_percent",
    "selected_total_cost",
    "effective_usd_per_percent",
    "capacity_lower_usd",
    "capacity_upper_usd",
)


class RequestLedger:
    """Price each request once; answer repeated time-window totals in O(log n)."""

    def __init__(self, account, config, now):
        self.account, self.config, self.now = account, config, now
        self.started_at = None
        self.rows = []
        self.times = []
        self.amounts, self.unknowns = [ZERO], [0]
        self.gpt_amounts, self.gpt_unknowns = [ZERO], [0]

    def _ensure_window(self, start):
        if self.started_at is not None and start >= self.started_at:
            return
        query = CPAUsageEvent.objects.filter(
            account=self.account,
            occurred_at__gte=start,
            occurred_at__lte=self.now,
        )
        if self.started_at is not None:
            query = query.filter(occurred_at__lt=self.started_at)
        events = (
            query.order_by("occurred_at", "id")
            .values(*EVENT_FIELDS)
            .iterator(chunk_size=2000)
        )
        older_rows = []
        for data in events:
            event = SimpleNamespace(pk=data["id"], **data)
            cost, unknown = cpa_event_cost(event, self.config)
            older_rows.append((event, cost, unknown))
        self.rows = older_rows + self.rows
        self.started_at = start
        self.times = []
        self.amounts, self.unknowns = [ZERO], [0]
        self.gpt_amounts, self.gpt_unknowns = [ZERO], [0]
        for event, cost, unknown in self.rows:
            self.times.append(event.occurred_at)
            self.amounts.append(self.amounts[-1] + cost)
            self.unknowns.append(self.unknowns[-1] + int(unknown))
            is_gpt = event.source == "gpt_load"
            self.gpt_amounts.append(self.gpt_amounts[-1] + (cost if is_gpt else ZERO))
            self.gpt_unknowns.append(self.gpt_unknowns[-1] + int(is_gpt and unknown))

    def bounds(self, start, end, *, include_start=True, include_end=True):
        self._ensure_window(start)
        left = (bisect_left if include_start else bisect_right)(self.times, start)
        right = (bisect_right if include_end else bisect_left)(self.times, end)
        return left, max(left, right)

    def between(self, start, end, **kwargs):
        left, right = self.bounds(start, end, **kwargs)
        return (self.rows[i] for i in range(left, right))

    def totals(self, start, end, *, gpt_load_only=False, **kwargs):
        left, right = self.bounds(start, end, **kwargs)
        amounts = self.gpt_amounts if gpt_load_only else self.amounts
        unknowns = self.gpt_unknowns if gpt_load_only else self.unknowns
        return amounts[right] - amounts[left], unknowns[right] - unknowns[left]


class SummaryProjection:
    def __init__(self, config, now, bindings=None):
        self.config, self.now, self.bindings = config, now, bindings
        self._ledgers = {}
        self._observations = {}
        self._intervals = {}
        self._coverage = {}
        self._cycles = {}
        self._discrepancies = {}
        self._summaries = {}

    def ledger(self, account):
        if account.id not in self._ledgers:
            self._ledgers[account.id] = RequestLedger(account, self.config, self.now)
        return self._ledgers[account.id]

    def observations(self, account):
        if account.id not in self._observations:
            # Reporting needs three JSON scalars, not every upstream response and
            # replay diagnostic. Use DTOs, never partially loaded mutable models.
            query = (
                Observation.objects.filter(
                    account_id=account.fact_key, observed_at__lte=self.now
                )
                .order_by("observed_at", "id")
                .values(
                    *OBSERVATION_FIELDS,
                    quota_provider=F("raw_window__provider"),
                    segment_reason=F("raw_window__replay_segment_reason"),
                    algorithm=F("model_diagnostics__algorithm"),
                )
            )
            rows = []
            for data in query.iterator(chunk_size=1000):
                raw_window = {
                    "provider": data.pop("quota_provider"),
                    "replay_segment_reason": data.pop("segment_reason"),
                }
                diagnostics = {"algorithm": data.pop("algorithm")}
                rows.append(
                    SimpleNamespace(
                        pk=data["id"],
                        raw_window=raw_window,
                        model_diagnostics=diagnostics,
                        **data,
                    )
                )
            self._observations[account.id] = rows
        return self._observations[account.id]

    def coverage(self, account, start, end):
        key = (account.id, start, end)
        if key not in self._coverage:
            if account.id not in self._intervals:
                self._intervals[account.id] = list(
                    account.cpa_collection_intervals.order_by("connected_at", "id")
                )
            self._coverage[key] = coverage_data(
                account, start, end, intervals=self._intervals[account.id]
            )
        return self._coverage[key]

    def discrepancy(self, account):
        from .quota_discrepancy import current_quota_discrepancy

        if account.id not in self._discrepancies:
            self._discrepancies[account.id] = current_quota_discrepancy(
                account, self.config, self.now, projection=self
            )
        return self._discrepancies[account.id]

    def summary(self, account):
        from .reporting import account_summary

        if account.id not in self._summaries:
            self._summaries[account.id] = account_summary(
                account, self.config, self.now, self.bindings, projection=self
            )
        return self._summaries[account.id]

    def cycles(self, account):
        from .billing import cycle_rows

        if account.id not in self._cycles:
            self._cycles[account.id] = cycle_rows(
                account, self.config, self.now, projection=self
            )
        # Billing appends future cycles. Do not let it mutate the shared history.
        return [dict(row) for row in self._cycles[account.id]]
