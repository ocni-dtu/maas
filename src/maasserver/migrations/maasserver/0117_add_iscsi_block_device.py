# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import maasserver.models.iscsiblockdevice


class Migration(migrations.Migration):

    dependencies = [("maasserver", "0116_add_disabled_components_for_mirrors")]

    operations = [
        migrations.CreateModel(
            name="ISCSIBlockDevice",
            fields=[
                (
                    "blockdevice_ptr",
                    models.OneToOneField(
                        parent_link=True,
                        auto_created=True,
                        primary_key=True,
                        to="maasserver.BlockDevice",
                        serialize=False,
                        on_delete=models.CASCADE,
                    ),
                ),
                (
                    "target",
                    models.CharField(
                        validators=[
                            maasserver.models.iscsiblockdevice.validate_iscsi_target
                        ],
                        max_length=4096,
                        unique=True,
                    ),
                ),
            ],
            bases=("maasserver.blockdevice",),
        )
    ]
