import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_latest_tier1_state(apps, schema_editor):
    Decision = apps.get_model('kbs', 'KBSDecision')
    SafetyState = apps.get_model('kbs', 'Tier1SafetyState')
    organization_ids = (
        Decision.objects.filter(tier='tier1')
        .order_by()
        .values_list('organization_id', flat=True)
        .distinct()
    )
    for organization_id in organization_ids:
        confirmed = list(
            Decision.objects.filter(
                organization_id=organization_id, tier='tier1',
                event_type__in=('decision', 'clear'),
            )
            .order_by('occurred_at', 'received_at', 'id')
        )
        if not confirmed:
            continue
        latest = confirmed[-1]
        active = latest.event_type == 'decision' and bool(latest.branch)
        commands = []
        if active:
            episode = []
            for decision in reversed(confirmed):
                if (
                    decision.event_type != 'decision'
                    or decision.branch != latest.branch
                ):
                    break
                episode.append(decision)
            by_device = {}
            for decision in reversed(episode):
                for action in decision.actions.order_by('created_at', 'id'):
                    by_device[action.device_id] = {
                        'device_id': action.device_id,
                        'action': action.action,
                        'countdown_s': action.countdown_s,
                        'reason': action.reason,
                    }
            commands = list(by_device.values())
        SafetyState.objects.create(
            organization_id=organization_id,
            edge_device_id=latest.edge_device_id,
            source_decision_id=latest.id,
            active=active,
            situation=latest.branch if active else '',
            episode_id=uuid.uuid4() if active else None,
            commands=commands,
            source_occurred_at=latest.occurred_at,
            activated_at=latest.occurred_at if active else None,
            cleared_at=latest.occurred_at if not active else None,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('kbs', '0007_canonical_tier2_engine'),
    ]

    operations = [
        migrations.AlterField(
            model_name='breakeraction',
            name='status',
            field=models.CharField(
                choices=[
                    ('pending', 'Pending'),
                    ('scheduled', 'Scheduled'),
                    ('applied', 'Applied'),
                    ('blocked', 'Blocked'),
                    ('failed', 'Failed'),
                    ('noop', 'No-op'),
                    ('suppressed_duplicate', 'Suppressed duplicate'),
                    ('superseded', 'Superseded'),
                ],
                default='pending', max_length=30,
            ),
        ),
        migrations.CreateModel(
            name='Tier1SafetyState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('active', models.BooleanField(default=False)),
                ('situation', models.CharField(blank=True, max_length=100)),
                ('episode_id', models.UUIDField(blank=True, editable=False, null=True)),
                ('commands', models.JSONField(default=list)),
                ('source_occurred_at', models.DateTimeField(blank=True, null=True)),
                ('activated_at', models.DateTimeField(blank=True, null=True)),
                ('cleared_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('edge_device', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='safety_states', to='kbs.edgedevice')),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='tier1_safety_state', to='organizations.organization')),
                ('source_decision', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='safety_state_updates', to='kbs.kbsdecision')),
            ],
        ),
        migrations.RunPython(backfill_latest_tier1_state, migrations.RunPython.noop),
    ]
