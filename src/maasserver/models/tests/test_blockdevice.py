# Copyright 2014-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `BlockDevice`."""

__all__ = []

import random
from unittest.mock import MagicMock, sentinel

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from maasserver.enum import FILESYSTEM_GROUP_TYPE, FILESYSTEM_TYPE
from maasserver.models import (
    BlockDevice,
    blockdevice as blockdevice_module,
    FilesystemGroup,
    ISCSIBlockDevice,
    PhysicalBlockDevice,
    VirtualBlockDevice,
    VolumeGroup,
)
from maasserver.models.iscsiblockdevice import validate_iscsi_target
from maasserver.models.partition import PARTITION_ALIGNMENT_SIZE
from maasserver.permissions import NodePermission
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.matchers import MockCalledWith
from maastesting.testcase import MAASTestCase
from testtools import ExpectedException
from testtools.matchers import Equals


class TestValidateISCSITarget(MAASTestCase):
    """Tests for the `validate_iscsi_target`."""

    def test__raises_no_errors_with_iscsi_prefix(self):
        host = factory.make_ipv4_address()
        target_name = factory.make_name("target")
        validate_iscsi_target("iscsi:%s::::%s" % (host, target_name))


class TestBlockDeviceManagerGetBlockDeviceOr404(MAASServerTestCase):
    """Tests for the `BlockDeviceManager.get_block_device_or_404`."""

    def test__raises_Http404_when_invalid_node(self):
        user = factory.make_admin()
        block_device = factory.make_BlockDevice()
        self.assertRaises(
            Http404,
            BlockDevice.objects.get_block_device_or_404,
            factory.make_name("system_id"),
            block_device.id,
            user,
            NodePermission.view,
        )

    def test__raises_Http404_when_invalid_device(self):
        user = factory.make_admin()
        node = factory.make_Node()
        # The call to make_Node creates a block device.  We'll pick a random id
        # larger than the Id in question, and it should fail to find it.
        dev_id = node.blockdevice_set.first().id
        self.assertRaises(
            Http404,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            random.randint(dev_id + 1, dev_id + 100),
            user,
            NodePermission.view,
        )

    def test__return_block_device_by_name(self):
        user = factory.make_User()
        node = factory.make_Node()
        device = factory.make_PhysicalBlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.name, user, NodePermission.view
            ).id,
        )

    def test__view_raises_PermissionDenied_when_user_not_owner(self):
        user = factory.make_User()
        node = factory.make_Node(owner=factory.make_User())
        device = factory.make_BlockDevice(node=node)
        self.assertRaises(
            PermissionDenied,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            device.id,
            user,
            NodePermission.view,
        )

    def test__view_returns_device_when_no_owner(self):
        user = factory.make_User()
        node = factory.make_Node()
        device = factory.make_PhysicalBlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.id, user, NodePermission.view
            ).id,
        )

    def test__view_returns_device_when_owner(self):
        user = factory.make_User()
        node = factory.make_Node(owner=user)
        device = factory.make_PhysicalBlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.id, user, NodePermission.view
            ).id,
        )

    def test__edit_raises_PermissionDenied_when_user_not_owner(self):
        user = factory.make_User()
        node = factory.make_Node(owner=factory.make_User())
        device = factory.make_BlockDevice(node=node)
        self.assertRaises(
            PermissionDenied,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            device.id,
            user,
            NodePermission.edit,
        )

    def test__edit_returns_device_when_user_is_owner(self):
        user = factory.make_User()
        node = factory.make_Node(owner=user)
        device = factory.make_BlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.id, user, NodePermission.edit
            ).id,
        )

    def test__admin_raises_PermissionDenied_when_user_requests_admin(self):
        user = factory.make_User()
        node = factory.make_Node()
        device = factory.make_BlockDevice(node=node)
        self.assertRaises(
            PermissionDenied,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            device.id,
            user,
            NodePermission.admin,
        )

    def test__admin_returns_device_when_admin(self):
        user = factory.make_admin()
        node = factory.make_Node()
        device = factory.make_BlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.id, user, NodePermission.admin
            ).id,
        )


class TestBlockDeviceManager(MAASServerTestCase):
    """Tests for the `BlockDeviceManager`."""

    def test__raises_Http404_when_invalid_node(self):
        user = factory.make_admin()
        block_device = factory.make_BlockDevice()
        self.assertRaises(
            Http404,
            BlockDevice.objects.get_block_device_or_404,
            factory.make_name("system_id"),
            block_device.id,
            user,
            NodePermission.view,
        )

    def test__raises_Http404_when_invalid_device(self):
        user = factory.make_admin()
        node = factory.make_Node()
        # The call to make_Node creates a block device.  We'll pick a random id
        # larger than the Id in question, and it should fail to find it.
        dev_id = node.blockdevice_set.first().id
        self.assertRaises(
            Http404,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            random.randint(dev_id + 1, dev_id + 100),
            user,
            NodePermission.view,
        )

    def test__returns_device_when_admin(self):
        user = factory.make_admin()
        node = factory.make_Node()
        device = factory.make_BlockDevice(node=node)
        self.assertEqual(
            device.id,
            BlockDevice.objects.get_block_device_or_404(
                node.system_id, device.id, user, NodePermission.admin
            ).id,
        )

    def test__raises_PermissionDenied_when_user_requests_admin(self):
        user = factory.make_User()
        node = factory.make_Node()
        device = factory.make_BlockDevice(node=node)
        self.assertRaises(
            PermissionDenied,
            BlockDevice.objects.get_block_device_or_404,
            node.system_id,
            device.id,
            user,
            NodePermission.admin,
        )

    def test_filter_by_tags_returns_devices_with_one_tag(self):
        tags = [factory.make_name("tag") for _ in range(3)]
        other_tags = [factory.make_name("tag") for _ in range(3)]
        devices_with_tags = [
            factory.make_BlockDevice(tags=tags) for _ in range(3)
        ]
        for _ in range(3):
            factory.make_BlockDevice(tags=other_tags)
        self.assertItemsEqual(
            devices_with_tags, BlockDevice.objects.filter_by_tags([tags[0]])
        )

    def test_filter_by_tags_returns_devices_with_all_tags(self):
        tags = [factory.make_name("tag") for _ in range(3)]
        other_tags = [factory.make_name("tag") for _ in range(3)]
        devices_with_tags = [
            factory.make_BlockDevice(tags=tags) for _ in range(3)
        ]
        for _ in range(3):
            factory.make_BlockDevice(tags=other_tags)
        self.assertItemsEqual(
            devices_with_tags, BlockDevice.objects.filter_by_tags(tags)
        )

    def test_filter_by_tags_returns_no_devices(self):
        tags = [factory.make_name("tag") for _ in range(3)]
        for _ in range(3):
            factory.make_BlockDevice(tags=tags)
        self.assertItemsEqual(
            [], BlockDevice.objects.filter_by_tags([factory.make_name("tag")])
        )

    def test_filter_by_tags_returns_devices_with_iterable(self):
        tags = [factory.make_name("tag") for _ in range(3)]
        other_tags = [factory.make_name("tag") for _ in range(3)]
        devices_with_tags = [
            factory.make_BlockDevice(tags=tags) for _ in range(3)
        ]
        for _ in range(3):
            factory.make_BlockDevice(tags=other_tags)

        def tag_generator():
            for tag in tags:
                yield tag

        self.assertItemsEqual(
            devices_with_tags,
            BlockDevice.objects.filter_by_tags(tag_generator()),
        )

    def test_filter_by_tags_raise_ValueError_when_unicode(self):
        self.assertRaises(
            ValueError, BlockDevice.objects.filter_by_tags, "test"
        )

    def test_filter_by_tags_raise_ValueError_when_not_iterable(self):
        self.assertRaises(
            ValueError, BlockDevice.objects.filter_by_tags, object()
        )

    def test_get_free_block_devices_for_node(self):
        node = factory.make_Node(with_boot_disk=False)
        free_devices = [factory.make_BlockDevice(node=node) for _ in range(3)]
        # Block devices with partition tables.
        for _ in range(3):
            factory.make_PartitionTable(
                block_device=factory.make_BlockDevice(node=node)
            )
        # Block devices with filesystems.
        for _ in range(3):
            factory.make_Filesystem(
                block_device=factory.make_BlockDevice(node=node)
            )
        self.assertItemsEqual(
            free_devices,
            BlockDevice.objects.get_free_block_devices_for_node(node),
        )

    def test_get_block_devices_in_filesystem_group(self):
        node = factory.make_Node()
        filesystem_group = factory.make_FilesystemGroup(
            group_type=FILESYSTEM_GROUP_TYPE.LVM_VG
        )
        block_devices = [
            filesystem.block_device
            for filesystem in filesystem_group.filesystems.all()
            if filesystem.block_device is not None
        ]
        block_device_with_partitions = factory.make_PhysicalBlockDevice(
            node=node
        )
        partition_table = factory.make_PartitionTable(
            block_device=block_device_with_partitions
        )
        partition = factory.make_Partition(partition_table=partition_table)
        factory.make_Filesystem(
            fstype=FILESYSTEM_TYPE.LVM_PV,
            partition=partition,
            filesystem_group=filesystem_group,
        )
        block_devices_in_filesystem_group = BlockDevice.objects.get_block_devices_in_filesystem_group(
            filesystem_group
        )
        self.assertItemsEqual(block_devices, block_devices_in_filesystem_group)
        self.assertNotIn(
            block_device_with_partitions, block_devices_in_filesystem_group
        )


class TestBlockDevice(MAASServerTestCase):
    """Tests for the `BlockDevice` model."""

    def test_path(self):
        block_device = factory.make_PhysicalBlockDevice()
        self.assertEqual(
            "/dev/disk/by-dname/%s" % block_device.name, block_device.path
        )

    def test_type_iscsi(self):
        block_device = factory.make_ISCSIBlockDevice()
        self.assertEqual("iscsi", block_device.type)

    def test_type_physical(self):
        block_device = factory.make_PhysicalBlockDevice()
        self.assertEqual("physical", block_device.type)

    def test_type_virtual(self):
        block_device = factory.make_VirtualBlockDevice()
        self.assertEqual("virtual", block_device.type)

    def test_type_raise_ValueError(self):
        block_device = factory.make_BlockDevice()
        with ExpectedException(ValueError):
            block_device.type

    def test_actual_instance_returns_ISCSIBlockDevice(self):
        block_device = factory.make_ISCSIBlockDevice()
        parent_type = BlockDevice.objects.get(id=block_device.id)
        self.assertIsInstance(parent_type.actual_instance, ISCSIBlockDevice)

    def test_actual_instance_returns_PhysicalBlockDevice(self):
        block_device = factory.make_PhysicalBlockDevice()
        parent_type = BlockDevice.objects.get(id=block_device.id)
        self.assertIsInstance(parent_type.actual_instance, PhysicalBlockDevice)

    def test_actual_instance_returns_VirtualBlockDevice(self):
        block_device = factory.make_VirtualBlockDevice()
        parent_type = BlockDevice.objects.get(id=block_device.id)
        self.assertIsInstance(parent_type.actual_instance, VirtualBlockDevice)

    def test_actual_instance_returns_BlockDevice(self):
        block_device = factory.make_BlockDevice()
        self.assertIsInstance(block_device.actual_instance, BlockDevice)

    def test_get_effective_filesystem(self):
        mock_get_effective_filesystem = self.patch_autospec(
            blockdevice_module, "get_effective_filesystem"
        )
        mock_get_effective_filesystem.return_value = sentinel.filesystem
        node = factory.make_Node(with_boot_disk=False)
        block_device = factory.make_BlockDevice(node=node)
        self.assertEqual(
            sentinel.filesystem, block_device.get_effective_filesystem()
        )

    def test_display_size(self):
        sizes = (
            (45, "45 bytes"),
            (1000, "1.0 kB"),
            (1000 * 1000, "1.0 MB"),
            (1000 * 1000 * 500, "500.0 MB"),
            (1000 * 1000 * 1000, "1.0 GB"),
            (1000 * 1000 * 1000 * 1000, "1.0 TB"),
        )
        block_device = BlockDevice()
        for (size, display_size) in sizes:
            block_device.size = size
            self.expectThat(block_device.display_size(), Equals(display_size))

    def test_get_name(self):
        name = factory.make_name("name")
        block_device = BlockDevice(name=name)
        self.assertEqual(name, block_device.get_name())

    def test_add_tag_adds_new_tag(self):
        block_device = BlockDevice()
        tag = factory.make_name("tag")
        block_device.add_tag(tag)
        self.assertItemsEqual([tag], block_device.tags)

    def test_add_tag_doesnt_duplicate(self):
        block_device = BlockDevice()
        tag = factory.make_name("tag")
        block_device.add_tag(tag)
        block_device.add_tag(tag)
        self.assertItemsEqual([tag], block_device.tags)

    def test_remove_tag_deletes_tag(self):
        block_device = BlockDevice()
        tag = factory.make_name("tag")
        block_device.add_tag(tag)
        block_device.remove_tag(tag)
        self.assertItemsEqual([], block_device.tags)

    def test_remove_tag_doesnt_error_on_missing_tag(self):
        block_device = BlockDevice()
        tag = factory.make_name("tag")
        #: Test is this doesn't raise an exception
        block_device.remove_tag(tag)

    def test_negative_size(self):
        node = factory.make_Node()
        blockdevice = BlockDevice(
            node=node, name="sda", block_size=512, size=-1
        )
        self.assertRaises(ValidationError, blockdevice.save)

    def test_minimum_size(self):
        node = factory.make_Node()
        blockdevice = BlockDevice(
            node=node, name="sda", block_size=512, size=143359
        )
        self.assertRaises(ValidationError, blockdevice.save)

    def test_negative_block_device_size(self):
        node = factory.make_Node()
        blockdevice = BlockDevice(
            node=node, name="sda", block_size=-1, size=143360
        )
        self.assertRaises(ValidationError, blockdevice.save)

    def test_minimum_block_device_size(self):
        node = factory.make_Node()
        blockdevice = BlockDevice(
            node=node, name="sda", block_size=511, size=143360
        )
        self.assertRaises(ValidationError, blockdevice.save)

    def test_get_partition_table_returns_none_for_non_partitioned_device(self):
        blockdevice = BlockDevice()
        self.assertIsNone(blockdevice.get_partitiontable())

    def test_delete_not_allowed_if_part_of_filesystem_group(self):
        block_device = factory.make_BlockDevice()
        VolumeGroup.objects.create_volume_group(
            factory.make_name("vg"), [block_device], []
        )
        error = self.assertRaises(ValidationError, block_device.delete)
        self.assertEqual(
            "Cannot delete block device because its part of a volume group.",
            error.message,
        )

    def test_delete(self):
        block_device = factory.make_BlockDevice()
        block_device.delete()
        self.assertIsNone(reload_object(block_device))

    def test_create_partition(self):
        node = factory.make_Node(with_boot_disk=False)
        disk = factory.make_PhysicalBlockDevice(node=node)
        partition = disk.create_partition()
        self.assertEqual(partition.partition_table.block_device, disk)
        available_size = disk.get_available_size()
        self.assertTrue(
            available_size >= 0 and available_size < PARTITION_ALIGNMENT_SIZE,
            "Should create a partition for the entire disk.",
        )

    def test_create_partition_raises_ValueError(self):
        disk = factory.make_PhysicalBlockDevice(node=factory.make_Node())
        factory.make_PartitionTable(block_device=disk)
        with ExpectedException(ValueError):
            disk.create_partition()

    def test_create_partition_if_boot_disk_returns_None_if_not_boot_disk(self):
        node = factory.make_Node()
        not_boot_disk = factory.make_PhysicalBlockDevice(node=node)
        self.assertIsNone(not_boot_disk.create_partition_if_boot_disk())

    def test_create_partition_if_boot_disk_creates_partition(self):
        node = factory.make_Node(with_boot_disk=False)
        boot_disk = factory.make_PhysicalBlockDevice(node=node)
        partition = boot_disk.create_partition_if_boot_disk()
        self.assertIsNotNone(partition)
        available_size = boot_disk.get_available_size()
        self.assertTrue(
            available_size >= 0 and available_size < PARTITION_ALIGNMENT_SIZE,
            "Should create a partition for the entire disk.",
        )


class TestBlockDeviceBlockNameIdx(MAASTestCase):

    scenarios = (
        ("0", {"idx": 0, "name": "sda"}),
        ("25", {"idx": 25, "name": "sdz"}),
        ("26", {"idx": 26, "name": "sdaa"}),
        ("27", {"idx": 27, "name": "sdab"}),
        ("51", {"idx": 51, "name": "sdaz"}),
        ("52", {"idx": 52, "name": "sdba"}),
        ("53", {"idx": 53, "name": "sdbb"}),
        ("701", {"idx": 701, "name": "sdzz"}),
        ("702", {"idx": 702, "name": "sdaaa"}),
        ("703", {"idx": 703, "name": "sdaab"}),
        ("18277", {"idx": 18277, "name": "sdzzz"}),
    )

    def test__get_block_name_from_idx(self):
        self.assertEqual(
            self.name, BlockDevice._get_block_name_from_idx(self.idx)
        )

    def test__get_idx_from_block_name(self):
        self.assertEqual(
            self.idx, BlockDevice._get_idx_from_block_name(self.name)
        )


class TestBlockDevicePostSaveCallsSave(MAASServerTestCase):
    """Tests for the `BlockDevice` post_save signal to call save on group."""

    scenarios = [
        ("BlockDevice", {"factory": factory.make_BlockDevice}),
        ("PhysicalBlockDevice", {"factory": factory.make_PhysicalBlockDevice}),
        ("VirtualBlockDevice", {"factory": factory.make_VirtualBlockDevice}),
    ]

    def test__calls_save_on_related_filesystem_groups(self):
        mock_filter_by_block_device = self.patch(
            FilesystemGroup.objects, "filter_by_block_device"
        )
        mock_filesystem_group = MagicMock()
        mock_filter_by_block_device.return_value = [mock_filesystem_group]
        self.factory()
        self.assertThat(mock_filesystem_group.save, MockCalledWith())


class TestBlockDevicePostSaveUpdatesName(MAASServerTestCase):
    """Tests for the `BlockDevice` post_save signal to update group name."""

    def test__updates_filesystem_group_name_when_not_volume_group(self):
        filesystem_group = factory.make_FilesystemGroup(
            group_type=factory.pick_enum(
                FILESYSTEM_GROUP_TYPE, but_not=FILESYSTEM_GROUP_TYPE.LVM_VG
            )
        )
        virtual_device = filesystem_group.virtual_device
        newname = factory.make_name("name")
        virtual_device.name = newname
        virtual_device.save()
        self.assertEqual(newname, reload_object(filesystem_group).name)

    def test__doesnt_update_filesystem_group_name_when_volume_group(self):
        virtual_device = factory.make_VirtualBlockDevice()
        filesystem_group = virtual_device.filesystem_group
        group_name = filesystem_group.name
        newname = factory.make_name("name")
        virtual_device.name = newname
        virtual_device.save()
        self.assertEqual(group_name, reload_object(filesystem_group).name)


class TestBlockDevicePostDelete(MAASServerTestCase):
    """Tests for the `BlockDevice` post_delete signal."""

    def test__deletes_filesystem_group_when_virtual_block_device_deleted(self):
        filesystem_group = factory.make_FilesystemGroup(
            group_type=factory.pick_enum(
                FILESYSTEM_GROUP_TYPE, but_not=FILESYSTEM_GROUP_TYPE.LVM_VG
            )
        )
        filesystem_group.virtual_device.delete()
        self.assertIsNone(reload_object(filesystem_group))

    def test__doesnt_delete_volume_group(self):
        virtual_device = factory.make_VirtualBlockDevice()
        volume_group = virtual_device.filesystem_group
        virtual_device.delete()
        self.assertIsNotNone(reload_object(volume_group))
