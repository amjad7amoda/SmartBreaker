import uuid

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def backfill_legacy_records(apps, schema_editor):
    Decision = apps.get_model('kbs', 'KBSDecision')
    Action = apps.get_model('kbs', 'BreakerAction')
    for decision in Decision.objects.all().iterator():
        decision.event_id = uuid.uuid4()
        decision.tier = 'tier2'
        decision.event_type = 'decision'
        decision.engine = 'legacy.apps.kbs.services.run_cycle'
        decision.trace_version = 0
        decision.trace = []
        decision.occurred_at = decision.created_at
        decision.save(update_fields=[
            'event_id', 'tier', 'event_type', 'engine', 'trace_version', 'trace',
            'occurred_at',
        ])
    for action in Action.objects.select_related('breaker').all().iterator():
        action.action_id = uuid.uuid4()
        action.device_id = action.breaker.device_id if action.breaker_id else ''
        action.status = 'applied' if action.executed else 'pending'
        action.executed_at = action.created_at if action.executed else None
        action.save(update_fields=[
            'action_id', 'device_id', 'status', 'executed_at',
        ])


class Migration(migrations.Migration):
    dependencies = [
        ('breakers', '0004_merge_kbs_tuya'),
        ('kbs', '0005_kbssettings_grid_present_min_v_alter_alert_kind_and_more'),
        ('organizations', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='EdgeDevice',
            fields=[
                ('device_id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(default='Primary edge', max_length=100)),
                ('secret_hash', models.CharField(max_length=128)),
                ('status', models.CharField(choices=[('active', 'Active'), ('revoked', 'Revoked')], default='active', max_length=20)),
                ('last_seen_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField('alert', 'decision', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='alerts', to='kbs.kbsdecision')),
        migrations.AddField('alert', 'suppressed', models.BooleanField(default=False)),
        migrations.AddField('alert', 'suppression_reason', models.CharField(blank=True, max_length=255)),
        # Nullable/non-unique first: callable defaults are evaluated only once
        # when a field is added, so populated installations need a per-row pass.
        migrations.AddField('breakeraction', 'action_id', models.UUIDField(editable=False, null=True)),
        migrations.AddField('breakeraction', 'device_id', models.CharField(default='', max_length=100), preserve_default=False),
        migrations.AddField('breakeraction', 'executed_at', models.DateTimeField(blank=True, null=True)),
        migrations.AddField('breakeraction', 'failure_reason', models.CharField(blank=True, max_length=500)),
        migrations.AddField('breakeraction', 'resulting_state', models.BooleanField(blank=True, null=True)),
        migrations.AddField('breakeraction', 'status', models.CharField(choices=[('pending', 'Pending'), ('scheduled', 'Scheduled'), ('applied', 'Applied'), ('blocked', 'Blocked'), ('failed', 'Failed'), ('noop', 'No-op'), ('suppressed_duplicate', 'Suppressed duplicate')], default='pending', max_length=30)),
        migrations.AddField('kbsdecision', 'engine', models.CharField(default='apps.kbs.engine.rules.decide', max_length=150)),
        migrations.AddField('kbsdecision', 'event_id', models.UUIDField(editable=False, null=True)),
        migrations.AddField('kbsdecision', 'event_type', models.CharField(choices=[('decision', 'Decision'), ('clear', 'Clear'), ('error', 'Error')], default='decision', max_length=20)),
        migrations.AddField('kbsdecision', 'occurred_at', models.DateTimeField(default=django.utils.timezone.now)),
        migrations.AddField('kbsdecision', 'received_at', models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now), preserve_default=False),
        migrations.AddField('kbsdecision', 'tier', models.CharField(choices=[('tier1', 'Tier 1'), ('tier2', 'Tier 2')], default='tier2', max_length=10)),
        migrations.AddField('kbsdecision', 'trace', models.JSONField(default=list)),
        migrations.AddField('kbsdecision', 'trace_version', models.PositiveSmallIntegerField(default=1)),
        migrations.RunPython(backfill_legacy_records, migrations.RunPython.noop),
        migrations.AlterField('breakeraction', 'action_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
        migrations.AlterField('kbsdecision', 'event_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
        migrations.AlterField('breakeraction', 'breaker', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='kbs_actions', to='breakers.breaker')),
        migrations.AlterField('kbsdecision', 'branch', models.CharField(blank=True, max_length=100)),
        migrations.AlterModelOptions(name='kbsdecision', options={'ordering': ['-occurred_at', '-received_at']}),
        migrations.AddIndex('breakeraction', models.Index(fields=['status', '-created_at'], name='kbs_breaker_status_c892b5_idx')),
        migrations.AddField('edgedevice', 'organization', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='edge_devices', to='organizations.organization')),
        migrations.AddField('kbsdecision', 'edge_device', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='decision_events', to='kbs.edgedevice')),
        migrations.AddIndex('kbsdecision', models.Index(fields=['organization', 'tier', '-occurred_at'], name='kbs_kbsdeci_organiz_396f5c_idx')),
        migrations.AddIndex('kbsdecision', models.Index(fields=['event_type', '-occurred_at'], name='kbs_kbsdeci_event_t_3533ee_idx')),
        migrations.AlterUniqueTogether(name='edgedevice', unique_together={('organization', 'name')}),
    ]
