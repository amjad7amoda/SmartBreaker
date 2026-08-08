# Hand-written: replaces the boolean use_data_clock with the richer
# data_source choice (real | simulator), preserving the old flag's meaning.

from django.db import migrations, models


def clock_flag_to_source(apps, schema_editor):
    """Sites that used the data clock were simulator-driven."""
    KBSSettings = apps.get_model('kbs', 'KBSSettings')
    KBSSettings.objects.filter(use_data_clock=True).update(data_source='simulator')


def source_to_clock_flag(apps, schema_editor):
    KBSSettings = apps.get_model('kbs', 'KBSSettings')
    KBSSettings.objects.filter(data_source='simulator').update(use_data_clock=True)


class Migration(migrations.Migration):

    dependencies = [
        ('kbs', '0003_kbssettings_use_data_clock'),
    ]

    operations = [
        migrations.AddField(
            model_name='kbssettings',
            name='data_source',
            field=models.CharField(
                choices=[('real', 'Real Site'), ('simulator', 'Simulator')],
                default='real', max_length=20,
            ),
        ),
        migrations.RunPython(clock_flag_to_source, source_to_clock_flag),
        migrations.RemoveField(model_name='kbssettings', name='use_data_clock'),
    ]
