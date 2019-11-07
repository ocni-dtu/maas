# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Node objects."""

__all__ = ["Tag"]

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import RegexValidator
from django.db.models import CharField, Manager, TextField
from django.shortcuts import get_object_or_404
from lxml import etree
from maasserver import DefaultMeta
from maasserver.models.cleansave import CleanSave
from maasserver.models.timestampedmodel import TimestampedModel
from maasserver.utils.orm import post_commit_do
from maasserver.utils.threads import deferToDatabase
from twisted.internet import reactor


class TagManager(Manager):
    """A utility to manage the collection of Tags."""

    # Everyone can see all tags, but only superusers can edit tags.

    def get_tag_or_404(self, name, user, to_edit=False):
        """Fetch a `Tag` by name.  Raise exceptions if no `Tag` with
        this name exist.

        :param name: The Tag.name.
        :type name: string
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param to_edit: Are we going to edit this tag, or just view it?
        :type to_edit: bool
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        if to_edit and not user.is_superuser:
            raise PermissionDenied()
        return get_object_or_404(Tag, name=name)


class Tag(CleanSave, TimestampedModel):
    """A `Tag` is a label applied to a `Node`.

    :ivar name: The short-human-identifiable name for this tag.
    :ivar definition: The XPATH string identifying what nodes should match this
        tag.
    :ivar comment: A long-form description for humans about what this tag is
        trying to accomplish.
    :ivar kernel_opts: Optional kernel command-line parameters string to be
        used in the PXE config for nodes with this tags.
    :ivar objects: The :class:`TagManager`.
    """

    _tag_name_regex = "^[a-zA-Z0-9_-]+$"

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

    name = CharField(
        max_length=256,
        unique=True,
        editable=True,
        validators=[RegexValidator(_tag_name_regex)],
    )
    definition = TextField(blank=True)
    comment = TextField(blank=True)
    kernel_opts = TextField(blank=True, null=True)

    objects = TagManager()

    def __init__(self, *args, **kwargs):
        super(Tag, self).__init__(*args, **kwargs)
        # Track what the original definition is, so we can detect when it
        # changes and we need to repopulate the node<=>tag mapping.
        # We have to check for self.id, otherwise we don't see the creation of
        # a new definition.
        if self.id is None:
            self._original_definition = None
        else:
            self._original_definition = self.definition

    def __str__(self):
        return self.name

    def _populate_nodes_later(self):
        """Find all nodes that match this tag, and update them, later.

        This schedules population to happen post-commit, without waiting for
        its outcome.
        """
        # Avoid circular imports.
        from maasserver.populate_tags import populate_tags

        if self.is_defined:
            # Schedule repopulate to happen after commit. This thread does not
            # wait for it to complete.
            post_commit_do(
                reactor.callLater, 0, deferToDatabase, populate_tags, self
            )

    def _populate_nodes_now(self):
        """Find all nodes that match this tag, and update them, now.

        All computation will be done within the current transaction, within
        the current thread. This could be costly.
        """
        # Avoid circular imports.
        from maasserver.populate_tags import populate_tag_for_multiple_nodes
        from maasserver.models.node import Node

        if self.is_defined:
            # Do the work here and now in this thread. This is probably a
            # terrible mistake... unless you're testing.
            populate_tag_for_multiple_nodes(self, Node.objects.all())

    def populate_nodes(self):
        """Find all nodes that match this tag, and update them.

        By default, node population is deferred.
        """
        return self._populate_nodes_later()

    def clean_definition(self):
        if self.is_defined:
            try:
                etree.XPath(self.definition)
            except etree.XPathSyntaxError as e:
                msg = "Invalid XPath expression: %s" % (e,)
                raise ValidationError({"definition": [msg]})

    def clean(self):
        self.clean_definition()

    def save(self, *args, populate=True, **kwargs):
        """Save this tag.

        :param populate: Whether or not to call `populate_nodes` if the
            definition has changed.
        """
        super(Tag, self).save(*args, **kwargs)
        if populate and (self.definition != self._original_definition):
            self.populate_nodes()
        self._original_definition = self.definition

    @property
    def is_defined(self):
        return (
            self.definition is not None
            and self.definition != ""
            and not self.definition.isspace()
        )
