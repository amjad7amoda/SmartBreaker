import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('kbs', '0008_tier1_safety_interlock'),
    ]

    operations = [
        migrations.AddField(
            model_name='kbssettings',
            name='tier2_policy',
            field=models.CharField(
                choices=[
                    ('crisp', 'Crisp'),
                    ('fuzzy_shadow', 'Fuzzy shadow'),
                    ('fuzzy_active', 'Fuzzy active'),
                ],
                default='crisp',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='kbsdecision',
            name='policy',
            field=models.CharField(
                choices=[
                    ('crisp', 'Crisp'),
                    ('fuzzy_shadow', 'Fuzzy shadow'),
                    ('fuzzy_active', 'Fuzzy active'),
                ],
                default='crisp',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='kbsdecision',
            name='counterfactual',
            field=models.JSONField(default=dict),
        ),
        migrations.CreateModel(
            name='KBSControllerState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('current_band', models.CharField(choices=[('low', 'Low'), ('watch', 'Watch'), ('high', 'High')], default='watch', max_length=10)),
                ('candidate_band', models.CharField(blank=True, choices=[('low', 'Low'), ('watch', 'Watch'), ('high', 'High')], default='', max_length=10)),
                ('consecutive_cycles', models.PositiveSmallIntegerField(default=0)),
                ('last_risk_score', models.FloatField(blank=True, null=True)),
                ('last_evaluated_at', models.DateTimeField(blank=True, null=True)),
                ('profile_version', models.CharField(default='mamdani-v1', max_length=64)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('organization', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='kbs_controller_state', to='organizations.organization')),
            ],
        ),
    ]
