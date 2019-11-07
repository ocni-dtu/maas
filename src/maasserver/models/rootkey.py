# Copyright 2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RootKey model."""

__all__ = ["RootKey"]

from django.db.models import BigAutoField, BinaryField, DateTimeField
from maasserver.models.timestampedmodel import TimestampedModel


class RootKey(TimestampedModel):
    """A root key for signing macaroons."""

    id = BigAutoField(primary_key=True, verbose_name="ID")
    material = BinaryField()
    expiration = DateTimeField()
