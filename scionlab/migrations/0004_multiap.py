# Generated by Django 2.2.4 on 2019-11-20 07:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scionlab', '0003_installation_types'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='useras',
            name='attachment_point',
        ),
        migrations.RemoveField(
            model_name='useras',
            name='bind_ip',
        ),
        migrations.RemoveField(
            model_name='useras',
            name='bind_port',
        ),
        migrations.RemoveField(
            model_name='useras',
            name='public_ip',
        ),
        migrations.AlterField(
            model_name='host',
            name='bind_ip',
            field=models.GenericIPAddressField(blank=True, help_text='Default bind IP for border router interfaces running on this host.', null=True),
        ),
        migrations.AlterField(
            model_name='host',
            name='public_ip',
            field=models.GenericIPAddressField(blank=True, help_text='Default public IP for border router interfaces running on this host.', null=True),
        ),
    ]