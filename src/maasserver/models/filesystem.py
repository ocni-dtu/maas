# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model for a filesystem on a partition or a block device."""

__all__ = ["Filesystem"]


from uuid import uuid4

from django.core.exceptions import ValidationError
from django.db.models import (
    BooleanField,
    CASCADE,
    CharField,
    ForeignKey,
    Manager,
    Q,
)
from maasserver import DefaultMeta
from maasserver.enum import (
    FILESYSTEM_FORMAT_TYPE_CHOICES_DICT,
    FILESYSTEM_TYPE,
    FILESYSTEM_TYPE_CHOICES,
)
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.cacheset import CacheSet
from maasserver.models.cleansave import CleanSave
from maasserver.models.filesystemgroup import FilesystemGroup
from maasserver.models.partition import Partition
from maasserver.models.timestampedmodel import TimestampedModel


class FilesystemManager(Manager):
    """Manager for `Filesystem` class."""

    def filter_by_node(self, node):
        """Return all filesystems on this node."""
        return self.filter(
            Q(node=node)
            | Q(block_device__node=node)
            | Q(partition__partition_table__block_device__node=node)
        )


class Filesystem(CleanSave, TimestampedModel):
    """A filesystem on partition or a block device.

    :ivar uuid: UUID of the filesystem.
    :ivar fstype: Type of filesystem. This can even be filesystems that
        cannot be mounted directly, e.g. LVM.
    :ivar partition: `Partition` this filesystem is on. If empty the filesystem
        must be directly on a `BlockDevice`.
    :ivar block_device: `BlockDevice` this filesystem is on. If empty the
        filesystem must be on a `Partition`.
    :ivar filesystem_group: `FilesystemGroup` this filesystem belongs to.
    :ivar create_params: Parameters that can be passed during the `mkfs`
        command when the filesystem is created.
    :ivar mount_point: Path to where this filesystem is mounted on the deployed
        operating system.
    :ivar mount_options: Parameters that are used to mount this filesystem
        on the deployed operating system.
    """

    # All filesystem types.
    TYPES = frozenset(fstype for fstype, _ in FILESYSTEM_TYPE_CHOICES)

    # Filesystem types that expect to be mounted into the host's filesystem.
    # Essentially this means all filesystems except swap.
    TYPES_REQUIRING_MOUNT_POINT = frozenset(
        fstype
        for fstype, _ in FILESYSTEM_TYPE_CHOICES
        if fstype != FILESYSTEM_TYPE.SWAP
    )

    # Filesystem types that require storage on a block special device, i.e. a
    # block device or partition.
    TYPES_REQUIRING_STORAGE = frozenset(
        fstype
        for fstype, _ in FILESYSTEM_TYPE_CHOICES
        if fstype != FILESYSTEM_TYPE.RAMFS and fstype != FILESYSTEM_TYPE.TMPFS
    )

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

        unique_together = (
            ("partition", "acquired"),
            ("block_device", "acquired"),
        )

    objects = FilesystemManager()

    uuid = CharField(
        max_length=36, unique=False, null=False, blank=False, editable=False
    )

    fstype = CharField(
        max_length=20,
        choices=FILESYSTEM_TYPE_CHOICES,
        default=FILESYSTEM_TYPE.EXT4,
    )

    partition = ForeignKey(
        Partition, unique=False, null=True, blank=True, on_delete=CASCADE
    )

    block_device = ForeignKey(
        BlockDevice, unique=False, null=True, blank=True, on_delete=CASCADE
    )

    node = ForeignKey(
        "Node",
        unique=False,
        null=True,
        blank=True,
        related_name="special_filesystems",
        on_delete=CASCADE,
    )

    # XXX: For CharField, why allow null *and* blank? Would
    # CharField(null=False, blank=True, default="") not work better?
    label = CharField(max_length=255, null=True, blank=True)

    filesystem_group = ForeignKey(
        FilesystemGroup,
        null=True,
        blank=True,
        related_name="filesystems",
        on_delete=CASCADE,
    )

    # XXX: For CharField, why allow null *and* blank? Would
    # CharField(null=False, blank=True, default="") not work better?
    create_params = CharField(max_length=255, null=True, blank=True)

    # XXX: For CharField, why allow null *and* blank? Would
    # CharField(null=False, blank=True, default="") not work better?
    mount_point = CharField(max_length=255, null=True, blank=True)

    # XXX: For CharField, why allow null *and* blank? Would
    # CharField(null=False, blank=True, default="") not work better?
    mount_options = CharField(max_length=255, null=True, blank=True)

    cache_set = ForeignKey(
        CacheSet,
        null=True,
        blank=True,
        related_name="filesystems",
        on_delete=CASCADE,
    )

    # When a node is allocated all Filesystem objects assigned to that node
    # with mountable filesystems will be duplicated with this field set to
    # True. This allows a standard user to change this object as they want
    # and format other free devices. Once the node is released these objects
    # will be deleted.
    acquired = BooleanField(default=False)

    def get_node(self):
        """`Node` this filesystem belongs to."""
        if self.partition is not None:
            return self.partition.get_node()
        elif self.block_device is not None:
            return self.block_device.node
        elif self.node is not None:
            return self.node
        else:
            # XXX: Explode instead?
            return None

    def get_size(self):
        """Size of filesystem."""
        if self.partition is not None:
            return self.partition.size
        elif self.block_device is not None:
            return self.block_device.size
        else:
            # XXX: Return None instead?
            return 0

    def get_block_size(self):
        """Block size of partition table."""
        if self.partition is not None:
            return self.partition.get_block_size()
        elif self.block_device is not None:
            return self.block_device.block_size
        else:
            # XXX: Return None instead?
            return 0

    def get_parent(self):
        """Return linked `BlockDevice` or linked `Partition`."""
        if self.partition is not None:
            return self.partition
        elif self.block_device is not None:
            return self.block_device.actual_instance
        elif self.node is not None:
            return self.node
        else:
            # XXX: Explode instead?
            return None

    @property
    def is_mountable(self):
        """Return True if this is a mountable filesystem."""
        return self.fstype in FILESYSTEM_FORMAT_TYPE_CHOICES_DICT

    @property
    def is_mounted(self):
        """Return True if this filesystem is mounted."""
        return self.mount_point is not None

    @property
    def uses_mount_point(self):
        """True if this filesystem can be mounted on a path.

        Swap partitions, for example, are not mounted at a particular point in
        the host's filesystem.
        """
        return self.fstype in self.TYPES_REQUIRING_MOUNT_POINT

    @property
    def uses_storage(self):
        """True if this filesystem expects a block special device.

        ramfs and tmpfs, for example, exist only in memory.
        """
        return self.fstype in self.TYPES_REQUIRING_STORAGE

    def clean(self, *args, **kwargs):
        super(Filesystem, self).clean(*args, **kwargs)
        parents = self.partition, self.block_device, self.node

        # You have to specify either a partition, block device, or node.
        if parents.count(None) == len(parents):
            if self.uses_storage:
                raise ValidationError(
                    "One of partition or block device must be specified."
                )
            else:
                raise ValidationError("A node must be specified.")

        # You can have only one of partition, block device, or node.
        if len(parents) - parents.count(None) > 1:
            raise ValidationError(
                "Only one of partition, block device, or node can "
                "be specified."
            )

        # If fstype is for a bcache as a cache device it needs to be in a
        # cache_set.
        if (
            self.fstype == FILESYSTEM_TYPE.BCACHE_CACHE
            and self.cache_set is None
        ):
            raise ValidationError(
                # XXX: Message leaks implementation details ("BCACHE_CACHE",
                # "cache_set").
                "BCACHE_CACHE must be inside of a cache_set."
            )

        # Normalise the mount point to None or "none" if this filesystem does
        # not use it. The mount point (fs_file) field in fstab(5) is ignored
        # for filesystems that don't have a mount point (i.e. swap) and "none"
        # should be used, so it's used here too. When the mount point is set
        # to None (rather than the string "none") it means that the filesystem
        # is unmounted. This overloading is going to catch us out one day.
        if not self.uses_mount_point:
            if self.mount_point is not None:
                self.mount_point = "none"

        # You cannot place a filesystem directly on the boot_disk. It requires
        # a partition to be used.
        if self.block_device is not None:
            node = self.block_device.node
            boot_disk = node.get_boot_disk()
            if boot_disk is not None and boot_disk.id == self.block_device.id:
                # This is the boot disk for the node.
                raise ValidationError(
                    "Cannot place filesystem directly on the boot disk. "
                    "Create a partition on the boot disk first and then "
                    "format the partition."
                )

        # Only ramfs and tmpfs can have a node as a parent.
        if self.uses_storage:
            if self.node is not None:
                raise ValidationError(
                    "A %s filesystem must be placed on a "
                    "block device or partition." % self.fstype
                )
        else:
            if self.node is None:
                raise ValidationError(
                    "RAM-backed filesystems cannot be placed on "
                    "block devices or partitions."
                )

        # Non-storage filesystems MUST be mounted.
        if (not self.uses_storage) and (not self.is_mounted):
            raise ValidationError("RAM-backed filesystems must be mounted.")

        # There should be no duplicate mount points.
        if self.is_mounted and self.uses_mount_point:
            # Find another filesystem that's mounted at the same point.
            owning_node_other_matching_mount_point = (
                Filesystem.objects.filter_by_node(self.get_node())
                .filter(mount_point=self.mount_point, acquired=self.acquired)
                .exclude(id=self.id)
            )
            if owning_node_other_matching_mount_point.exists():
                raise ValidationError(
                    "Another filesystem is already mounted at %s."
                    % (self.mount_point,)
                )

    def save(self, *args, **kwargs):
        if not self.uuid:
            self.uuid = uuid4()
        super(Filesystem, self).save(*args, **kwargs)
