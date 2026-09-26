from django.db import migrations, models


class Migration(migrations.Migration):
    # GPT-Load was already deployed from its branch before the main feature
    # migrations were merged. Keep the combined upgrade graph linear so table
    # rebuilds in later main migrations retain the GPT-Load fields.
    dependencies = [("monitor", "0059_cpa_quota_adjustments")]

    operations = [
        migrations.AddField(
            model_name="appsettings",
            name="auto_apply_recommendations",
            field=models.BooleanField(default=False),
        ),
    ]
