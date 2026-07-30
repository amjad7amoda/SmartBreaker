# Hand-written: renames preserve existing data (type -> load_type,
# priority -> priority_degree, peak_load -> peak_load_W, mean_load -> mean_load_W);
# `protected` is replaced by priority_type='mandatory'.

import django.db.models.deletion
from django.db import migrations, models


def protected_to_mandatory(apps, schema_editor):
    """Carry the old `protected` flag over into the new priority_type field."""
    Breaker = apps.get_model('breakers', 'Breaker')
    Breaker.objects.filter(protected=True).update(priority_type='mandatory')


class Migration(migrations.Migration):

    dependencies = [
        ('breakers', '0001_initial'),
    ]

    operations = [
        migrations.RenameField(model_name='breaker', old_name='type', new_name='load_type'),
        migrations.RenameField(model_name='breaker', old_name='priority', new_name='priority_degree'),
        migrations.RenameField(model_name='breaker', old_name='peak_load', new_name='peak_load_W'),
        migrations.RenameField(model_name='breaker', old_name='mean_load', new_name='mean_load_W'),
        migrations.AlterField(
            model_name='breaker',
            name='load_type',
            field=models.CharField(choices=[('motor', 'Motor'), ('normal', 'Normal')], default='normal', max_length=20),
        ),
        migrations.AlterField(
            model_name='breaker',
            name='priority_degree',
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name='breaker',
            name='peak_load_W',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='breaker',
            name='mean_load_W',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='breaker',
            name='priority_type',
            field=models.CharField(
                choices=[('mandatory', 'Mandatory'), ('normal', 'Normal'), ('comfort', 'Comfort'), ('ac_grid', 'AC Grid')],
                default='normal', max_length=20,
            ),
        ),
        migrations.RunPython(protected_to_mandatory, migrations.RunPython.noop),
        migrations.RemoveField(model_name='breaker', name='protected'),
        migrations.AddField(
            model_name='breaker',
            name='locked_out',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='breaker',
            name='lockout_reason',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='breaker',
            name='locked_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name='breaker',
            options={'ordering': ['organization', 'priority_type', '-priority_degree']},
        ),
        migrations.CreateModel(
            name='BreakerStatus',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('switch', models.BooleanField(default=False)),
                ('countdown_1_s', models.PositiveIntegerField(default=0)),
                ('cur_current_mA', models.FloatField(blank=True, null=True)),
                ('cur_power_mW', models.FloatField(blank=True, null=True)),
                ('cur_voltage_mV', models.FloatField(blank=True, null=True)),
                ('fault', models.CharField(blank=True, default='', max_length=100)),
                ('relay_status', models.CharField(choices=[('power_off', 'Power Off'), ('power_on', 'Power On'), ('last', 'Last')], default='last', max_length=20)),
                ('child_lock', models.BooleanField(default=False)),
                ('cycle_time', models.CharField(blank=True, default='', max_length=100)),
                ('online', models.BooleanField(default=False)),
                ('last_switched_on_at', models.DateTimeField(blank=True, null=True)),
                ('reported_at', models.DateTimeField(auto_now=True)),
                ('breaker', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='status', to='breakers.breaker')),
            ],
            options={'verbose_name_plural': 'breaker statuses'},
        ),
        migrations.CreateModel(
            name='BreakerReading',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField()),
                ('switch', models.BooleanField()),
                ('cur_power_mW', models.FloatField(blank=True, null=True)),
                ('breaker', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='readings', to='breakers.breaker')),
            ],
            options={'ordering': ['-timestamp']},
        ),
        migrations.AddIndex(
            model_name='breakerreading',
            index=models.Index(fields=['breaker', '-timestamp'], name='breakers_br_breaker_1859b5_idx'),
        ),
        migrations.AddConstraint(
            model_name='breakerreading',
            constraint=models.UniqueConstraint(fields=('breaker', 'timestamp'), name='uniq_breaker_reading'),
        ),
    ]
