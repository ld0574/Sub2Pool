import uuid

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("monitor", "0058_remove_monitoredaccount_account_provider_identity_valid_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CPAQuotaAdjustmentPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("amount_usd", models.DecimalField(decimal_places=6, max_digits=18)),
                ("reason", models.CharField(max_length=500)),
                ("source_digest", models.CharField(max_length=64)),
                ("preview", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("expires_at", models.DateTimeField()),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_plans", to="monitor.monitoredaccount")),
                ("baseline_observation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_baseline_plans", to="monitor.observation")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("latest_observation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_latest_plans", to="monitor.observation")),
                ("participant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_plans", to="monitor.participant")),
            ],
        ),
        migrations.CreateModel(
            name="CPAQuotaAdjustment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("cycle_started_at", models.DateTimeField()),
                ("cycle_ended_at", models.DateTimeField()),
                ("effective_at", models.DateTimeField(db_index=True)),
                ("amount_usd", models.DecimalField(decimal_places=6, max_digits=18)),
                ("reason", models.CharField(max_length=500)),
                ("evidence", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("account", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustments", to="monitor.monitoredaccount")),
                ("baseline_observation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_baselines", to="monitor.observation")),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ("latest_observation", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustment_latest", to="monitor.observation")),
                ("participant", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="quota_adjustments", to="monitor.participant")),
                ("plan", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="adjustment", to="monitor.cpaquotaadjustmentplan")),
                ("reversal_of", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="reversal", to="monitor.cpaquotaadjustment")),
            ],
            options={"ordering": ["effective_at", "created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="cpaquotaadjustment",
            constraint=models.CheckConstraint(condition=~models.Q(("amount_usd", 0)), name="cpa_quota_adjustment_nonzero"),
        ),
        migrations.AddConstraint(
            model_name="cpaquotaadjustment",
            constraint=models.CheckConstraint(condition=models.Q(("cycle_ended_at__gt", models.F("cycle_started_at"))), name="cpa_quota_adjustment_positive_cycle"),
        ),
        migrations.AddIndex(
            model_name="cpaquotaadjustment",
            index=models.Index(fields=["account", "cycle_started_at", "cycle_ended_at"], name="monitor_cpa_account_d0dfdd_idx"),
        ),
    ]
