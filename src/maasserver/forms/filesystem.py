# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Forms relating to filesystems."""

__all__ = [
    "MountFilesystemForm",
    "MountNonStorageFilesystemForm",
    "UnmountNonStorageFilesystemForm",
]

from typing import Optional

from django.forms import ChoiceField, Form
from maasserver.enum import FILESYSTEM_FORMAT_TYPE_CHOICES
from maasserver.fields import StrippedCharField
from maasserver.forms import AbsolutePathField
from maasserver.models import Filesystem, Node
from provisioningserver.utils import typed


class MountFilesystemForm(Form):
    """Form used to mount a filesystem."""

    @typed
    def __init__(self, filesystem: Optional[Filesystem], *args, **kwargs):
        super(MountFilesystemForm, self).__init__(*args, **kwargs)
        self.filesystem = filesystem
        self.setup()

    def setup(self):
        if self.filesystem is not None:
            if self.filesystem.uses_mount_point:
                self.fields["mount_point"] = AbsolutePathField(required=True)
            self.fields["mount_options"] = StrippedCharField(required=False)

    def clean(self):
        cleaned_data = super(MountFilesystemForm, self).clean()
        if self.filesystem is None:
            self.add_error(
                None,
                "Cannot mount an unformatted partition " "or block device.",
            )
        elif self.filesystem.filesystem_group is not None:
            self.add_error(
                None,
                "Filesystem is part of a filesystem group, "
                "and cannot be mounted.",
            )
        return cleaned_data

    def save(self):
        if "mount_point" in self.cleaned_data:
            self.filesystem.mount_point = self.cleaned_data["mount_point"]
        else:
            self.filesystem.mount_point = "none"  # e.g. for swap.
        if "mount_options" in self.cleaned_data:
            self.filesystem.mount_options = self.cleaned_data["mount_options"]
        self.filesystem.save()


class MountNonStorageFilesystemForm(Form):
    """Form used to create and mount a non-storage filesystem."""

    mount_point = AbsolutePathField(required=True)
    mount_options = StrippedCharField(required=False)
    fstype = ChoiceField(
        required=True,
        choices=[
            (name, displayname)
            for name, displayname in FILESYSTEM_FORMAT_TYPE_CHOICES
            if name not in Filesystem.TYPES_REQUIRING_STORAGE
        ],
    )

    @typed
    def __init__(self, node: Node, *args, **kwargs):
        super(MountNonStorageFilesystemForm, self).__init__(*args, **kwargs)
        self.node = node

    @typed
    def save(self) -> Filesystem:
        filesystem = Filesystem(
            node=self.node,
            fstype=self.cleaned_data["fstype"],
            mount_options=self.cleaned_data["mount_options"],
            mount_point=self.cleaned_data["mount_point"],
            acquired=self.node.owner is not None,
        )
        filesystem.save()
        return filesystem


class UnmountNonStorageFilesystemForm(Form):
    """Form used to unmount and destroy a non-storage filesystem."""

    mount_point = AbsolutePathField(required=True)

    @typed
    def __init__(self, node: Node, *args, **kwargs):
        super(UnmountNonStorageFilesystemForm, self).__init__(*args, **kwargs)
        self.node = node

    def clean(self):
        cleaned_data = super(UnmountNonStorageFilesystemForm, self).clean()
        if "mount_point" in cleaned_data:
            try:
                self.filesystem = Filesystem.objects.get(
                    node=self.node, mount_point=cleaned_data["mount_point"]
                )
            except Filesystem.DoesNotExist:
                self.add_error(
                    "mount_point",
                    "No special filesystem is " "mounted at this path.",
                )
        return cleaned_data

    @typed
    def save(self) -> None:
        self.filesystem.delete()
