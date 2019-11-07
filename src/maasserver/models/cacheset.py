# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Model for a Bcache cache set."""

__all__ = ["CacheSet"]


from django.core.exceptions import PermissionDenied
from django.db.models import Manager, Q
from django.http import Http404
from maasserver import DefaultMeta
from maasserver.enum import FILESYSTEM_TYPE
from maasserver.models.cleansave import CleanSave
from maasserver.models.timestampedmodel import TimestampedModel


class CacheSetManager(Manager):
    def get_cache_set_idx(self, cache_set):
        """Return the idx of this cache set for its node."""
        node = cache_set.get_node()
        cache_sets = self.filter(
            Q(filesystems__partition__partition_table__block_device__node=node)
            | Q(filesystems__block_device__node=node)
        ).order_by("id")
        for idx, cset in enumerate(cache_sets):
            if cset == cache_set:
                return idx
        raise self.model.DoesNotExist()

    def get_cache_sets_for_node(self, node):
        """Return the cache sets for the `node`."""
        partition_filter = {
            "filesystems__partition__partition_table__"
            "block_device__node": node
        }
        return self.filter(
            Q(filesystems__block_device__node=node) | Q(**partition_filter)
        )

    def get_cache_set_for_block_device(self, block_device):
        """Return the cache set for `block_device`."""
        return self.filter(filesystems__block_device=block_device).first()

    def get_cache_set_for_partition(self, partition):
        """Return the cache set for `partition`."""
        return self.filter(filesystems__partition=partition).first()

    def get_or_create_cache_set_for_block_device(self, block_device):
        """Get or create the cache set for the `block_device`."""
        # Circular imports.
        from maasserver.models.filesystem import Filesystem

        existing_cache_set = self.get_cache_set_for_block_device(block_device)
        if existing_cache_set is not None:
            return existing_cache_set
        else:
            cache_set = self.create()
            Filesystem.objects.create(
                block_device=block_device,
                fstype=FILESYSTEM_TYPE.BCACHE_CACHE,
                cache_set=cache_set,
            )
            return cache_set

    def get_or_create_cache_set_for_partition(self, partition):
        """Get or create the cache set for the `partition`."""
        # Circular imports.
        from maasserver.models.filesystem import Filesystem

        existing_cache_set = self.get_cache_set_for_partition(partition)
        if existing_cache_set is not None:
            return existing_cache_set
        else:
            cache_set = self.create()
            Filesystem.objects.create(
                partition=partition,
                fstype=FILESYSTEM_TYPE.BCACHE_CACHE,
                cache_set=cache_set,
            )
            return cache_set

    def get_cache_set_by_id_or_name(self, cache_set_id_or_name, node):
        """Return cache set by its ID or name."""
        try:
            cache_set_id = int(cache_set_id_or_name)
        except ValueError:
            name_split = cache_set_id_or_name.split("cache")
            if len(name_split) != 2:
                # Invalid name.
                raise self.model.DoesNotExist()
            _, cache_number = name_split
            try:
                cache_number = int(cache_number)
            except ValueError:
                # Invalid cache number.
                raise self.model.DoesNotExist()
            cache_sets = self.get_cache_sets_for_node(node)
            for cache_set in cache_sets:
                if cache_number == self.get_cache_set_idx(cache_set):
                    return cache_set
            # No cache set with that name on the node.
            raise self.model.DoesNotExist()
        cache_set = self.get(id=cache_set_id)
        if cache_set.get_node() != node:
            raise self.model.DoesNotExist()
        else:
            return cache_set

    def get_cache_set_or_404(self, system_id, cache_set_id, user, perm):
        """Fetch a `CacheSet` by its `Node`'s system_id and its id.  Raise
        exceptions if no `CacheSet` with this id exist, if the `Node` with
        system_id doesn't exist, if the `CacheSet` doesn't exist on the
        `Node`, or if the provided user has not the required permission on
        this `Node` and `CacheSet`.

        :param name: The system_id.
        :type name: string
        :param name: The blockdevice_id.
        :type name: int
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param perm: The permission to assert that the user has on the node.
        :type perm: unicode
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        # Circular imports.
        from maasserver.models.node import Machine

        machine = Machine.objects.get_node_or_404(system_id, user, perm)
        try:
            cache_set = self.get_cache_set_by_id_or_name(cache_set_id, machine)
        except self.model.DoesNotExist:
            raise Http404()
        node = cache_set.get_node()
        if node.system_id != system_id:
            raise Http404()
        if user.has_perm(perm, node):
            return cache_set
        else:
            raise PermissionDenied()


class CacheSet(CleanSave, TimestampedModel):
    """A Bcache cache set."""

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

    objects = CacheSetManager()

    @property
    def name(self):
        """Return the name of the cache set."""
        return self.get_name()

    def get_node(self):
        """Return the node of the cache set."""
        device = self.get_device()
        if device is None:
            return None
        else:
            return device.get_node()

    def get_name(self):
        """Return the name of the node."""
        cache_idx = CacheSet.objects.get_cache_set_idx(self)
        return "cache%d" % cache_idx

    def get_filesystem(self):
        """Return the filesystem for this cache set."""
        return self.filesystems.first()

    def get_device(self):
        """Return the device that is apart of this cache set.

        Returns either a `ISCSIBlockDevice`, `PhysicalBlockDevice`,
        `VirtualBlockDevice`, or `Partition`.
        """
        filesystem = self.get_filesystem()
        if filesystem is None:
            return None
        else:
            return filesystem.get_parent()
