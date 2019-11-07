# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model that holds hint information for a Pod."""

__all__ = ["PodHints"]


from django.db.models import (
    BigIntegerField,
    CASCADE,
    IntegerField,
    Model,
    OneToOneField,
)
from maasserver import DefaultMeta
from maasserver.models.cleansave import CleanSave


class PodHints(CleanSave, Model):
    """Hint information for a pod."""

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

    pod = OneToOneField("BMC", related_name="hints", on_delete=CASCADE)

    cores = IntegerField(default=0)

    memory = IntegerField(default=0)

    cpu_speed = IntegerField(default=0)  # MHz

    local_storage = BigIntegerField(  # Bytes
        blank=False, null=False, default=0
    )

    local_disks = IntegerField(blank=False, null=False, default=-1)

    iscsi_storage = BigIntegerField(  # Bytes
        blank=False, null=False, default=-1
    )
