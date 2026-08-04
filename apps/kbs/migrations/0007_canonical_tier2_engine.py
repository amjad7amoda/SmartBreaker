from django.db import migrations, models


CANONICAL_ENGINE = 'apps.kbs.services.run_cycle'
RULE_ENGINE_ALIAS = 'apps.kbs.engine.rules.decide'


def canonicalize_traced_tier2_records(apps, schema_editor):
    Decision = apps.get_model('kbs', 'KBSDecision')
    Decision.objects.filter(
        tier='tier2',
        trace_version__gte=1,
        engine=RULE_ENGINE_ALIAS,
    ).update(engine=CANONICAL_ENGINE)


class Migration(migrations.Migration):
    dependencies = [
        ('kbs', '0006_decision_trace_audit'),
    ]

    operations = [
        migrations.RunPython(
            canonicalize_traced_tier2_records,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name='kbsdecision',
            name='engine',
            field=models.CharField(
                default=CANONICAL_ENGINE,
                max_length=150,
            ),
        ),
    ]
