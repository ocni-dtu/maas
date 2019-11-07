# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

__all__ = ["DHCPSnippet"]

from django.core.exceptions import ValidationError
from django.db.models import (
    BooleanField,
    CASCADE,
    CharField,
    ForeignKey,
    Manager,
    QuerySet,
    TextField,
)
from maasserver.models.cleansave import CleanSave
from maasserver.models.node import Node
from maasserver.models.subnet import Subnet
from maasserver.models.timestampedmodel import TimestampedModel
from maasserver.models.versionedtextfile import VersionedTextFile
from maasserver.utils.orm import MAASQueriesMixin


class DHCPSnippetQueriesMixin(MAASQueriesMixin):
    def get_specifiers_q(self, specifiers, separator=":", **kwargs):
        # This dict is used by the constraints code to identify objects
        # with particular properties. Please note that changing the keys here
        # can impact backward compatibility, so use caution.
        specifier_types = {
            None: self._add_default_query,
            "id": "__id",
            "name": "__name",
        }
        return super(DHCPSnippetQueriesMixin, self).get_specifiers_q(
            specifiers,
            specifier_types=specifier_types,
            separator=separator,
            **kwargs
        )


class DHCPSnippetQuerySet(QuerySet, DHCPSnippetQueriesMixin):
    """Custom QuerySet which mixes in some additional queries specific to
    this object. This needs to be a mixin because an identical method is needed
    on both the Manager and all QuerySets which result from calling the
    manager.
    """


class DHCPSnippetManager(Manager, DHCPSnippetQueriesMixin):
    def get_queryset(self):
        return DHCPSnippetQuerySet(self.model, using=self._db)

    def get_dhcp_snippet_or_404(self, specifiers):
        """Fetch a `DHCPSnippet` by its id. Raise exceptions if no
        `DHCPSnippet` with its id exists, or if the provided user does not
        have the required permission on this `DHCPSnippet`.

        :param specifiers: The interface specifier.
        :type specifiers: str
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        return self.get_object_by_specifiers_or_raise(specifiers)


class DHCPSnippet(CleanSave, TimestampedModel):

    name = CharField(max_length=255)

    value = ForeignKey(VersionedTextFile, on_delete=CASCADE)

    description = TextField(blank=True)

    enabled = BooleanField(default=True)

    # What the snippet is being used for. If the snippet isn't linked to
    # anything its a global snippet

    node = ForeignKey(Node, null=True, blank=True, on_delete=CASCADE)

    subnet = ForeignKey(Subnet, null=True, blank=True, on_delete=CASCADE)

    objects = DHCPSnippetManager()

    def __str__(self):
        return self.name

    def clean(self, *args, **kwargs):
        super().clean(*args, **kwargs)
        if self.node is not None and self.subnet is not None:
            raise ValidationError(
                "A DHCP snippet cannot be enabled on a node and subnet at the "
                "same time."
            )
