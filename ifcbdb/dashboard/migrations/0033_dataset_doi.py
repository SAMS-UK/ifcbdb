# Generated by Django 2.1.7 on 2019-10-31 16:30

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0032_auto_20191008_2022'),
    ]

    operations = [
        migrations.AddField(
            model_name='dataset',
            name='doi',
            field=models.CharField(blank=True, max_length=256),
        ),
    ]