# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model base class for view-backed models."""

__all__ = ["ViewModel"]

from django.db.models import Model
from maasserver import DefaultMeta


class ViewModel(Model):
    """Base class for a view-backed Django `Model`."""

    class Meta(DefaultMeta):
        abstract = True

    def save(self):
        raise NotImplementedError("Cannot save a view-backed model.")
