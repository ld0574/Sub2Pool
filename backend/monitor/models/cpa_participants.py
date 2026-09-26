"""CPA identities and immutable, time-scoped ownership/contract evidence."""

import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class CPAAPIKey(models.Model):
    key_hash = models.CharField(max_length=64, unique=True)
    hint = models.CharField(max_length=4)
    name = models.CharField(max_length=80, blank=True)
    source = models.CharField(
        max_length=16,
        choices=(("cpa", "CPA"), ("gpt_load", "GPT-Load")),
        default="cpa",
        db_index=True,
    )
    external_key_id = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_key_id"],
                condition=Q(external_key_id__isnull=False),
                name="unique_gateway_external_key",
            )
        ]


class CPAKeyBinding(models.Model):
    key = models.ForeignKey(
        CPAAPIKey, on_delete=models.PROTECT, related_name="bindings"
    )
    participant = models.ForeignKey(
        "Participant", on_delete=models.PROTECT, related_name="cpa_bindings"
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    claim = models.OneToOneField(
        "CPAClaimPlan", null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        ordering = ["started_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["key"],
                condition=Q(ended_at__isnull=True),
                name="one_open_cpa_key_binding",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gt=F("started_at")),
                name="cpa_binding_positive_interval",
            ),
        ]
        indexes = [models.Index(fields=["key", "started_at", "ended_at"])]


class CPAClaimPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.ForeignKey(CPAAPIKey, null=True, on_delete=models.PROTECT)
    account = models.ForeignKey("MonitoredAccount", null=True, on_delete=models.PROTECT)
    participant = models.ForeignKey(
        "Participant", on_delete=models.PROTECT, related_name="cpa_claim_plans"
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField()
    source_digest = models.CharField(max_length=64)
    preview = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    applied_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(key__isnull=False, account__isnull=True)
                    | Q(key__isnull=True, account__isnull=False)
                ),
                name="cpa_claim_single_source",
            )
        ]


class CPAQuotaContract(models.Model):
    """Frozen per-account pool policy; never backdate today's allocations."""

    account = models.ForeignKey(
        "MonitoredAccount", on_delete=models.PROTECT, related_name="cpa_contracts"
    )
    effective_at = models.DateTimeField()
    pool_id_at_capture = models.BigIntegerField()
    pool_name = models.CharField(max_length=160)
    revision = models.PositiveBigIntegerField()
    allocations = models.JSONField(default=list)

    class Meta:
        ordering = ["effective_at", "id"]
        indexes = [models.Index(fields=["account", "effective_at"])]


class CPAClaimEvent(models.Model):
    """A historical claim owns only the immutable events confirmed in its preview."""

    plan = models.ForeignKey(
        CPAClaimPlan, on_delete=models.PROTECT, related_name="events"
    )
    event = models.OneToOneField(
        "CPAUsageEvent", on_delete=models.PROTECT, related_name="ownership_claim"
    )


class CPAQuotaAdjustmentPlan(models.Model):
    """Short-lived, evidence-bound preview for a manual quota attribution."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "MonitoredAccount", on_delete=models.PROTECT, related_name="quota_adjustment_plans"
    )
    participant = models.ForeignKey(
        "Participant", on_delete=models.PROTECT, related_name="quota_adjustment_plans"
    )
    baseline_observation = models.ForeignKey(
        "Observation", on_delete=models.PROTECT, related_name="quota_adjustment_baseline_plans"
    )
    latest_observation = models.ForeignKey(
        "Observation", on_delete=models.PROTECT, related_name="quota_adjustment_latest_plans"
    )
    amount_usd = models.DecimalField(max_digits=18, decimal_places=6)
    reason = models.CharField(max_length=500)
    source_digest = models.CharField(max_length=64)
    preview = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    applied_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )


class CPAQuotaAdjustment(models.Model):
    """Immutable manual attribution; reversals are equal, opposite entries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "MonitoredAccount", on_delete=models.PROTECT, related_name="quota_adjustments"
    )
    participant = models.ForeignKey(
        "Participant", on_delete=models.PROTECT, related_name="quota_adjustments"
    )
    plan = models.OneToOneField(
        CPAQuotaAdjustmentPlan,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="adjustment",
    )
    reversal_of = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="reversal",
    )
    baseline_observation = models.ForeignKey(
        "Observation", on_delete=models.PROTECT, related_name="quota_adjustment_baselines"
    )
    latest_observation = models.ForeignKey(
        "Observation", on_delete=models.PROTECT, related_name="quota_adjustment_latest"
    )
    cycle_started_at = models.DateTimeField()
    cycle_ended_at = models.DateTimeField()
    effective_at = models.DateTimeField(db_index=True)
    amount_usd = models.DecimalField(max_digits=18, decimal_places=6)
    reason = models.CharField(max_length=500)
    evidence = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["effective_at", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(amount_usd=0), name="cpa_quota_adjustment_nonzero"
            ),
            models.CheckConstraint(
                condition=Q(cycle_ended_at__gt=F("cycle_started_at")),
                name="cpa_quota_adjustment_positive_cycle",
            ),
        ]
        indexes = [
            models.Index(fields=["account", "cycle_started_at", "cycle_ended_at"])
        ]


class CPAAccountOwnerBinding(models.Model):
    """Prospective fallback ownership. Explicit key and historical claims win."""

    account = models.ForeignKey(
        "MonitoredAccount", on_delete=models.PROTECT, related_name="cpa_owner_bindings"
    )
    participant = models.ForeignKey(
        "Participant", on_delete=models.PROTECT, related_name="cpa_owner_bindings"
    )
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["account"],
                condition=Q(ended_at__isnull=True),
                name="one_open_cpa_account_owner",
            ),
            models.CheckConstraint(
                condition=Q(ended_at__isnull=True) | Q(ended_at__gt=F("started_at")),
                name="cpa_owner_positive_interval",
            ),
        ]


class CPAQuotaResetRequest(models.Model):
    """One confirmed upstream redemption, retried with the same idempotency key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account = models.ForeignKey(
        "MonitoredAccount", on_delete=models.PROTECT, related_name="cpa_reset_requests"
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL
    )
    source_account_id = models.CharField(max_length=255)
    available_count = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="pending")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True)
