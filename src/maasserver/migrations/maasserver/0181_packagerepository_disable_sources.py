# -*- coding: utf-8 -*-
# Generated by Django 1.11.11 on 2018-10-19 08:12
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("maasserver", "0180_rbaclastsync")]

    operations = [
        migrations.AddField(
            model_name="packagerepository",
            name="disable_sources",
            field=models.BooleanField(default=True),
        )
    ]