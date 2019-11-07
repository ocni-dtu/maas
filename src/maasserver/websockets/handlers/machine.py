# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""The machine handler for the WebSocket connection."""

__all__ = ["MachineHandler"]

from functools import partial
import logging
from operator import itemgetter

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db.models import OuterRef, Subquery
from maasserver.enum import (
    BMC_TYPE,
    INTERFACE_LINK_TYPE,
    IPADDRESS_TYPE,
    NODE_STATUS,
    NODE_STATUS_CHOICES,
    POWER_STATE,
)
from maasserver.exceptions import NodeActionError, NodeStateViolation
from maasserver.forms import (
    AddPartitionForm,
    AdminMachineWithMACAddressesForm,
    CreateBcacheForm,
    CreateCacheSetForm,
    CreateLogicalVolumeForm,
    CreateRaidForm,
    CreateVMFSForm,
    CreateVolumeGroupForm,
    FormatBlockDeviceForm,
    FormatPartitionForm,
    UpdatePhysicalBlockDeviceForm,
    UpdateVirtualBlockDeviceForm,
    UpdateVMFSForm,
)
from maasserver.forms.filesystem import (
    MountFilesystemForm,
    MountNonStorageFilesystemForm,
    UnmountNonStorageFilesystemForm,
)
from maasserver.forms.interface import (
    AcquiredBridgeInterfaceForm,
    BondInterfaceForm,
    BridgeInterfaceForm,
    DeployedInterfaceForm,
    InterfaceForm,
    PhysicalInterfaceForm,
    VLANInterfaceForm,
)
from maasserver.forms.interface_link import InterfaceLinkForm
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.cacheset import CacheSet
from maasserver.models.event import Event
from maasserver.models.filesystem import Filesystem
from maasserver.models.filesystemgroup import VolumeGroup
from maasserver.models.interface import Interface
from maasserver.models.node import Machine, Node
from maasserver.models.partition import Partition
from maasserver.models.subnet import Subnet
from maasserver.node_action import compile_node_actions
from maasserver.permissions import NodePermission
from maasserver.storage_layouts import (
    StorageLayoutError,
    StorageLayoutForm,
    StorageLayoutMissingBootDiskError,
)
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maasserver.websockets.base import (
    HandlerDoesNotExistError,
    HandlerError,
    HandlerPermissionError,
    HandlerValidationError,
)
from maasserver.websockets.handlers.node import node_prefetch, NodeHandler
from metadataserver.enum import HARDWARE_TYPE, RESULT_TYPE
from provisioningserver.logger import LegacyLogger
from provisioningserver.rpc.exceptions import UnknownPowerType
from provisioningserver.utils.twisted import asynchronous


log = LegacyLogger()


class MachineHandler(NodeHandler):
    class Meta(NodeHandler.Meta):
        abstract = False
        queryset = (
            node_prefetch(Machine.objects.all())
            .prefetch_related(
                "blockdevice_set__iscsiblockdevice__"
                "partitiontable_set__partitions"
            )
            .prefetch_related(
                "blockdevice_set__physicalblockdevice__"
                "partitiontable_set__partitions__filesystem_set"
            )
            .prefetch_related(
                "blockdevice_set__virtualblockdevice__"
                "partitiontable_set__partitions"
            )
        )
        list_queryset = (
            Machine.objects.select_related("owner", "zone", "domain", "bmc")
            .prefetch_related(
                "blockdevice_set__iscsiblockdevice__"
                "partitiontable_set__partitions"
            )
            .prefetch_related(
                "blockdevice_set__physicalblockdevice__"
                "partitiontable_set__partitions"
            )
            .prefetch_related(
                "blockdevice_set__physicalblockdevice__numa_node"
            )
            .prefetch_related(
                "blockdevice_set__virtualblockdevice__"
                "partitiontable_set__partitions"
            )
            .prefetch_related(
                "interface_set__ip_addresses__subnet__vlan__space"
            )
            .prefetch_related(
                "interface_set__ip_addresses__subnet__vlan__fabric"
            )
            .prefetch_related("interface_set__numa_node")
            .prefetch_related("interface_set__vlan__fabric")
            .prefetch_related("boot_interface__vlan__fabric")
            .prefetch_related("tags")
            .prefetch_related("pool")
            .annotate(
                status_event_type_description=Subquery(
                    Event.objects.filter(
                        node=OuterRef("pk"), type__level__gte=logging.INFO
                    )
                    .order_by("-created", "-id")
                    .values("type__description")[:1]
                ),
                status_event_description=Subquery(
                    Event.objects.filter(
                        node=OuterRef("pk"), type__level__gte=logging.INFO
                    )
                    .order_by("-created", "-id")
                    .values("description")[:1]
                ),
            )
        )
        allowed_methods = [
            "list",
            "get",
            "create",
            "update",
            "action",
            "set_active",
            "check_power",
            "create_physical",
            "create_vlan",
            "create_bond",
            "create_bridge",
            "update_interface",
            "delete_interface",
            "link_subnet",
            "unlink_subnet",
            "mount_special",
            "unmount_special",
            "update_filesystem",
            "update_disk",
            "delete_disk",
            "delete_partition",
            "delete_volume_group",
            "delete_cache_set",
            "delete_filesystem",
            "delete_vmfs_datastore",
            "update_vmfs_datastore",
            "create_partition",
            "create_cache_set",
            "create_bcache",
            "create_raid",
            "create_volume_group",
            "create_logical_volume",
            "create_vmfs_datastore",
            "set_boot_disk",
            "apply_storage_layout",
            "default_user",
            "get_summary_xml",
            "get_summary_yaml",
            "set_script_result_suppressed",
            "set_script_result_unsuppressed",
            "get_suppressible_script_results",
            "get_latest_failed_testing_script_results",
        ]
        form = AdminMachineWithMACAddressesForm
        exclude = [
            "creation_type",
            "status_expires",
            "previous_status",
            "parent",
            "boot_interface",
            "boot_cluster_ip",
            "token",
            "netboot",
            "ephemeral_deploy",
            "agent_name",
            "power_state_queried",
            "power_state_updated",
            "gateway_link_ipv4",
            "gateway_link_ipv6",
            "enable_ssh",
            "skip_bmc_config",
            "skip_networking",
            "skip_storage",
            "instance_power_parameters",
            "address_ttl",
            "url",
            "dns_process",
            "managing_process",
            "last_image_sync",
            "install_rackd",
            "install_kvm",
        ]
        list_fields = [
            "id",
            "system_id",
            "hostname",
            "locked",
            "owner",
            "cpu_count",
            "cpu_speed",
            "description",
            "memory",
            "power_state",
            "domain",
            "pool",
            "zone",
        ]
        listen_channels = ["machine"]
        create_permission = NodePermission.admin
        view_permission = NodePermission.view
        edit_permission = NodePermission.admin
        delete_permission = NodePermission.admin

    def get_queryset(self, for_list=False):
        """Return `QuerySet` for devices only viewable by `user`."""
        return Machine.objects.get_nodes(
            self.user,
            NodePermission.view,
            from_nodes=super().get_queryset(for_list=for_list),
        )

    def dehydrate(self, obj, data, for_list=False):
        """Add extra fields to `data`."""
        data = super().dehydrate(obj, data, for_list=for_list)
        data.update(
            {"locked": obj.locked, "pool": self.dehydrate_pool(obj.pool)}
        )
        # Try to use the annotated event description so its loaded in the same
        # query as loading the machines. Otherwise fallback to the method on
        # the machine.
        if hasattr(obj, "status_event_description"):
            if obj.status_event_description:
                data["status_message"] = "%s - %s" % (
                    obj.status_event_type_description,
                    obj.status_event_description,
                )
            else:
                data["status_message"] = obj.status_event_type_description
        else:
            data["status_message"] = obj.status_message()

        if obj.is_machine or not for_list:
            boot_interface = obj.get_boot_interface()
            if boot_interface is not None:
                data["pxe_mac"] = "%s" % boot_interface.mac_address
                data["pxe_mac_vendor"] = obj.get_pxe_mac_vendor()
                data["power_type"] = obj.power_type
                data["vlan"] = self.dehydrate_vlan(obj, boot_interface)
                data["ip_addresses"] = self.dehydrate_all_ip_addresses(obj)
            else:
                data["pxe_mac"] = data["pxe_mac_vendor"] = ""

        # Needed for machines to show up in the Pod details page.
        if obj.bmc is not None and obj.bmc.bmc_type == BMC_TYPE.POD:
            data["pod"] = self.dehydrate_pod(obj.bmc)

        cpu_script_results = [
            script_result
            for script_result in self._script_results.get(obj.id, {}).get(
                HARDWARE_TYPE.CPU, []
            )
            if script_result.script_set.result_type == RESULT_TYPE.TESTING
        ]
        data["cpu_test_status"] = self.dehydrate_test_statuses(
            cpu_script_results
        )

        memory_script_results = [
            script_result
            for script_result in self._script_results.get(obj.id, {}).get(
                HARDWARE_TYPE.MEMORY, []
            )
            if script_result.script_set.result_type == RESULT_TYPE.TESTING
        ]
        data["memory_test_status"] = self.dehydrate_test_statuses(
            memory_script_results
        )

        storage_script_results = [
            script_result
            for script_result in self._script_results.get(obj.id, {}).get(
                HARDWARE_TYPE.STORAGE, []
            )
            if script_result.script_set.result_type == RESULT_TYPE.TESTING
        ]
        data["storage_test_status"] = self.dehydrate_test_statuses(
            storage_script_results
        )

        interface_script_results = [
            script_result
            for script_result in self._script_results.get(obj.id, {}).get(
                HARDWARE_TYPE.NETWORK, []
            )
            if script_result.script_set.result_type == RESULT_TYPE.TESTING
        ]
        data["interface_test_status"] = self.dehydrate_test_statuses(
            interface_script_results
        )

        node_script_results = [
            script_result
            for script_result in self._script_results.get(obj.id, {}).get(
                HARDWARE_TYPE.NODE, []
            )
            if script_result.script_set.result_type == RESULT_TYPE.TESTING
        ]
        data["other_test_status"] = self.dehydrate_test_statuses(
            node_script_results
        )

        if not for_list:
            # Add info specific to a machine.
            data["show_os_info"] = self.dehydrate_show_os_info(obj)
            devices = [
                self.dehydrate_device(device) for device in obj.children.all()
            ]
            data["devices"] = sorted(devices, key=itemgetter("fqdn"))

        return data

    def dehydrate_show_os_info(self, obj):
        """Return True if OS information should show in the UI."""
        return (
            obj.status == NODE_STATUS.DEPLOYING
            or obj.status == NODE_STATUS.FAILED_DEPLOYMENT
            or obj.status == NODE_STATUS.DEPLOYED
            or obj.status == NODE_STATUS.RELEASING
            or obj.status == NODE_STATUS.FAILED_RELEASING
            or obj.status == NODE_STATUS.DISK_ERASING
            or obj.status == NODE_STATUS.FAILED_DISK_ERASING
        )

    def dehydrate_device(self, device):
        """Return the `Device` formatted for JSON encoding."""
        return {
            "fqdn": device.fqdn,
            "interfaces": [
                self.dehydrate_interface(interface, device)
                for interface in device.interface_set.all()
            ],
        }

    def get_form_class(self, action):
        """Return the form class used for `action`."""
        if action in ("create", "update"):
            return AdminMachineWithMACAddressesForm
        else:
            raise HandlerError("Unknown action: %s" % action)

    def preprocess_form(self, action, params):
        """Process the `params` to before passing the data to the form."""
        new_params = {}

        # Only copy the allowed fields into `new_params` to be passed into
        # the form.
        new_params["mac_addresses"] = self.get_mac_addresses(params)
        new_params["hostname"] = params.get("hostname")
        new_params["architecture"] = params.get("architecture")
        new_params["description"] = params.get("description")
        new_params["power_type"] = params.get("power_type")
        new_params["power_parameters"] = params.get("power_parameters")
        if "zone" in params:
            new_params["zone"] = params["zone"]["name"]
        if params.get("pool"):
            new_params["pool"] = params["pool"]["name"]
        if "domain" in params:
            new_params["domain"] = params["domain"]["name"]
        if "min_hwe_kernel" in params:
            new_params["min_hwe_kernel"] = params["min_hwe_kernel"]

        # Cleanup any fields that have a None value.
        new_params = {
            key: value
            for key, value in new_params.items()
            if value is not None
        }

        return super().preprocess_form(action, new_params)

    def create(self, params):
        """Create the object from params."""
        data = super().create(params)
        node_obj = Node.objects.get(system_id=data["system_id"])

        # Start the commissioning process right away, which has the
        # desired side effect of initializing the node's power state.
        d = node_obj.start_commissioning(self.user)
        # Silently ignore errors to prevent tracebacks. The commissioning
        # callbacks have their own logging. This fixes LP1600328.
        d.addErrback(lambda _: None)

        return self.full_dehydrate(node_obj)

    def update(self, params):
        """Update the object from params."""
        data = super().update(params)
        node_obj = Node.objects.get(system_id=data["system_id"])

        # Update the tags for the node and disks if they are set.
        if "tags" in params:
            self.update_tags(node_obj, params["tags"])
        node_obj.save()

        return self.full_dehydrate(node_obj)

    def mount_special(self, params):
        """Mount a special-purpose filesystem, like tmpfs.

        :param fstype: The filesystem type. This must be a filesystem that
            does not require a block special device.
        :param mount_point: Path on the filesystem to mount.
        :param mount_option: Options to pass to mount(8).

        :attention: This is more or less a copy of `mount_special` from
            `m.api.machines`.
        """
        machine = self._get_node_or_permission_error(
            params, permission=NodePermission.edit
        )
        if machine.locked:
            raise HandlerPermissionError()
        self._preflight_special_filesystem_modifications("mount", machine)
        form = MountNonStorageFilesystemForm(machine, data=params)
        if form.is_valid():
            form.save()
        else:
            raise HandlerValidationError(form.errors)

    def unmount_special(self, params):
        """Unmount a special-purpose filesystem, like tmpfs.

        :param mount_point: Path on the filesystem to unmount.

        :attention: This is more or less a copy of `unmount_special` from
            `m.api.machines`.
        """
        machine = self._get_node_or_permission_error(
            params, permission=NodePermission.edit
        )
        if machine.locked:
            raise HandlerPermissionError()
        self._preflight_special_filesystem_modifications("unmount", machine)
        form = UnmountNonStorageFilesystemForm(machine, data=params)
        if form.is_valid():
            form.save()
        else:
            raise HandlerValidationError(form.errors)

    def _preflight_special_filesystem_modifications(self, op, machine):
        """Check that `machine` is okay for special fs modifications."""
        if self.user.has_perm(NodePermission.admin, machine):
            statuses_permitted = {NODE_STATUS.READY, NODE_STATUS.ALLOCATED}
        else:
            statuses_permitted = {NODE_STATUS.ALLOCATED}
        if machine.status not in statuses_permitted:
            status_names = sorted(
                title
                for value, title in NODE_STATUS_CHOICES
                if value in statuses_permitted
            )
            raise NodeStateViolation(
                "Cannot %s the filesystem because the machine is not %s."
                % (op, " or ".join(status_names))
            )

    def update_filesystem(self, params):
        node = self._get_node_or_permission_error(
            params, permission=NodePermission.edit
        )
        if node.locked:
            raise HandlerPermissionError()
        block_id = params.get("block_id")
        partition_id = params.get("partition_id")
        fstype = params.get("fstype")
        mount_point = params.get("mount_point")
        mount_options = params.get("mount_options")

        if node.status not in [NODE_STATUS.ALLOCATED, NODE_STATUS.READY]:
            raise HandlerError(
                "Node must be allocated or ready to edit storage"
            )

        # If this is on a block device, check if the tags need to be updated.
        # (The client sends them in from the same form.)
        blockdevice = None
        if block_id is not None:
            blockdevice = BlockDevice.objects.get(id=block_id, node=node)
            tags = params.get("tags", None)
            # If the tags parameter was left out, that means "don't touch the
            # tags". (An empty list means "clear the tags".)
            if tags is not None:
                tags = [tag["text"] for tag in tags]
                if set(blockdevice.tags) != set(tags):
                    blockdevice.tags = tags
                    blockdevice.save()
        if partition_id:
            self.update_partition_filesystem(
                node, partition_id, fstype, mount_point, mount_options
            )
        elif blockdevice is not None:
            self.update_blockdevice_filesystem(
                blockdevice, fstype, mount_point, mount_options
            )

    def update_partition_filesystem(
        self, node, partition_id, fstype, mount_point, mount_options
    ):
        partition = Partition.objects.get(
            id=partition_id, partition_table__block_device__node=node
        )
        fs = partition.get_effective_filesystem()
        if not fstype:
            if fs:
                fs.delete()
                return
        if fs is None or fstype != fs.fstype:
            form = FormatPartitionForm(partition, {"fstype": fstype})
            if not form.is_valid():
                raise HandlerError(form.errors)
            form.save()
            fs = partition.get_effective_filesystem()
        if mount_point != fs.mount_point:
            # XXX: Elsewhere, a mount_point of "" would somtimes mean that the
            # filesystem is mounted, sometimes that it is *not* mounted. Which
            # is correct was not clear from the code history, so the existing
            # behaviour is maintained here.
            if mount_point is None or mount_point == "":
                fs.mount_point = None
                fs.mount_options = None
                fs.save()
            else:
                form = MountFilesystemForm(
                    partition.get_effective_filesystem(),
                    {
                        "mount_point": mount_point,
                        "mount_options": mount_options,
                    },
                )
                if not form.is_valid():
                    raise HandlerError(form.errors)
                else:
                    form.save()

    def update_blockdevice_filesystem(
        self, blockdevice, fstype, mount_point, mount_options
    ):
        fs = blockdevice.get_effective_filesystem()
        if not fstype:
            if fs:
                fs.delete()
            return
        if fs is None or fstype != fs.fstype:
            form = FormatBlockDeviceForm(blockdevice, {"fstype": fstype})
            if not form.is_valid():
                raise HandlerError(form.errors)
            form.save()
            fs = blockdevice.get_effective_filesystem()
        if mount_point != fs.mount_point:
            # XXX: Elsewhere, a mount_point of "" would somtimes mean that the
            # filesystem is mounted, sometimes that it is *not* mounted. Which
            # is correct was not clear from the code history, so the existing
            # behaviour is maintained here.
            if mount_point is None or mount_point == "":
                fs.mount_point = None
                fs.mount_options = None
                fs.save()
            else:
                form = MountFilesystemForm(
                    blockdevice.get_effective_filesystem(),
                    {
                        "mount_point": mount_point,
                        "mount_options": mount_options,
                    },
                )
                if not form.is_valid():
                    raise HandlerError(form.errors)
                else:
                    form.save()

    def _update_obj_tags(self, obj, params):
        if "tags" in params:
            obj.tags = params["tags"]
            obj.save(update_fields=["tags"])

    def update_disk(self, params):
        """Update disk information."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        device = BlockDevice.objects.get(
            id=params["block_id"], node=node
        ).actual_instance
        if device.type == "physical":
            form = UpdatePhysicalBlockDeviceForm(instance=device, data=params)
        elif device.type == "virtual":
            form = UpdateVirtualBlockDeviceForm(instance=device, data=params)
        else:
            raise HandlerError(
                "Cannot update block device of type %s" % device.type
            )
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            disk_obj = form.save()
            self._update_obj_tags(disk_obj, params)
            if "fstype" in params:
                self.update_blockdevice_filesystem(
                    disk_obj,
                    params["fstype"],
                    params.get("mount_point", ""),
                    params.get("mount_options", ""),
                )

    def delete_disk(self, params):
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        block_id = params.get("block_id")
        if block_id is not None:
            block_device = BlockDevice.objects.get(id=block_id, node=node)
            block_device.delete()

    def delete_partition(self, params):
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        partition_id = params.get("partition_id")
        if partition_id is not None:
            partition = Partition.objects.get(
                id=partition_id, partition_table__block_device__node=node
            )
            partition.delete()

    def delete_volume_group(self, params):
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        volume_group_id = params.get("volume_group_id")
        if volume_group_id is not None:
            volume_group = VolumeGroup.objects.get(id=volume_group_id)
            if volume_group.get_node() != node:
                raise VolumeGroup.DoesNotExist()
            volume_group.delete()

    def delete_cache_set(self, params):
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        cache_set_id = params.get("cache_set_id")
        if cache_set_id is not None:
            cache_set = CacheSet.objects.get(id=cache_set_id)
            if cache_set.get_node() != node:
                raise CacheSet.DoesNotExist()
            cache_set.delete()

    def delete_filesystem(self, params):
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        blockdevice_id = params.get("blockdevice_id")
        partition_id = params.get("partition_id")
        filesystem_id = params.get("filesystem_id")
        if partition_id is None:
            blockdevice = BlockDevice.objects.get(node=node, id=blockdevice_id)
            fs = Filesystem.objects.get(
                block_device=blockdevice, id=filesystem_id
            )
        else:
            partition = Partition.objects.get(id=partition_id)
            fs = Filesystem.objects.get(partition=partition, id=filesystem_id)
        fs.delete()

    def _get_vmfs_datastore(self, params):
        """Get the VMFS datastore from the given system_id and id."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        vmfs_datastore_id = params.get("vmfs_datastore_id")
        try:
            vbd = node.virtualblockdevice_set.get(id=vmfs_datastore_id)
        except ObjectDoesNotExist:
            raise HandlerDoesNotExistError(vmfs_datastore_id)
        if not vbd.filesystem_group:
            raise HandlerDoesNotExistError(vmfs_datastore_id)
        return vbd.filesystem_group

    def delete_vmfs_datastore(self, params):
        """Delete a VMFS datastore."""
        vmfs = self._get_vmfs_datastore(params)
        vmfs.delete()

    def update_vmfs_datastore(self, params):
        """Add or remove block devices or partitions from a datastore."""
        vmfs = self._get_vmfs_datastore(params)
        form = UpdateVMFSForm(vmfs, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def create_partition(self, params):
        """Create a partition."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        disk_obj = BlockDevice.objects.get(id=params["block_id"], node=node)
        form = AddPartitionForm(disk_obj, {"size": params["partition_size"]})
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            partition = form.save()

        self._update_obj_tags(partition, params)
        if "fstype" in params:
            self.update_partition_filesystem(
                node,
                partition.id,
                params.get("fstype"),
                params.get("mount_point"),
                params.get("mount_options"),
            )

    def create_cache_set(self, params):
        """Create a cache set."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        block_id = params.get("block_id")
        partition_id = params.get("partition_id")

        data = {}
        if partition_id is not None:
            data["cache_partition"] = partition_id
        elif block_id is not None:
            data["cache_device"] = block_id
        else:
            raise HandlerError("Either block_id or partition_id is required.")

        form = CreateCacheSetForm(node=node, data=data)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def create_bcache(self, params):
        """Create a bcache."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        block_id = params.get("block_id")
        partition_id = params.get("partition_id")

        data = {
            "name": params["name"],
            "cache_set": params["cache_set"],
            "cache_mode": params["cache_mode"],
        }

        if partition_id is not None:
            data["backing_partition"] = partition_id
        elif block_id is not None:
            data["backing_device"] = block_id
        else:
            raise HandlerError("Either block_id or partition_id is required.")

        form = CreateBcacheForm(node=node, data=data)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            bcache = form.save()

        self._update_obj_tags(bcache.virtual_device, params)
        if "fstype" in params:
            self.update_blockdevice_filesystem(
                bcache.virtual_device,
                params.get("fstype"),
                params.get("mount_point"),
                params.get("mount_options"),
            )

    def create_raid(self, params):
        """Create a RAID."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = CreateRaidForm(node=node, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            raid = form.save()

        self._update_obj_tags(raid.virtual_device, params)
        if "fstype" in params:
            self.update_blockdevice_filesystem(
                raid.virtual_device,
                params.get("fstype"),
                params.get("mount_point"),
                params.get("mount_options"),
            )

    def create_volume_group(self, params):
        """Create a volume group."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = CreateVolumeGroupForm(node=node, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def create_logical_volume(self, params):
        """Create a logical volume."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        volume_group = VolumeGroup.objects.get(id=params["volume_group_id"])
        if volume_group.get_node() != node:
            raise VolumeGroup.DoesNotExist()
        form = CreateLogicalVolumeForm(
            volume_group, {"name": params["name"], "size": params["size"]}
        )
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            logical_volume = form.save()

        self._update_obj_tags(logical_volume, params)
        if "fstype" in params:
            self.update_blockdevice_filesystem(
                logical_volume,
                params.get("fstype"),
                params.get("mount_point"),
                params.get("mount_options"),
            )

    def create_vmfs_datastore(self, params):
        """Create a VMFS datastore."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = CreateVMFSForm(node, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        else:
            form.save()

    def set_boot_disk(self, params):
        """Set the disk as the boot disk."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        device = BlockDevice.objects.get(
            id=params["block_id"], node=node
        ).actual_instance
        if device.type != "physical":
            raise HandlerError(
                "Only a physical disk can be set as the boot disk."
            )
        node.boot_disk = device
        node.save()

    def apply_storage_layout(self, params):
        """Apply the specified storage layout."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = StorageLayoutForm(required=True, data=params)
        if not form.is_valid():
            raise HandlerError(form.errors)
        storage_layout = params.get("storage_layout")
        try:
            node.set_storage_layout(storage_layout)
        except StorageLayoutMissingBootDiskError:
            raise HandlerError(
                "Machine is missing a boot disk; no storage layout can be "
                "applied."
            )
        except StorageLayoutError as e:
            raise HandlerError(
                "Failed to configure storage layout '%s': %s"
                % (storage_layout, str(e))
            )

    def action(self, params):
        """Perform the action on the object."""
        # `compile_node_actions` handles the permission checking internally
        # the default view permission check is enough at this level.
        obj = self.get_object(params)
        action_name = params.get("action")
        actions = compile_node_actions(obj, self.user, request=self.request)
        action = actions.get(action_name)
        if action is None:
            raise NodeActionError(
                "%s action is not available for this node." % action_name
            )
        extra_params = params.get("extra", {})
        return action.execute(**extra_params)

    def _create_link_on_interface(self, interface, params):
        """Create a link on a new interface."""
        mode = params.get("mode", None)
        subnet_id = params.get("subnet", None)
        if mode is not None:
            if mode != INTERFACE_LINK_TYPE.LINK_UP:
                link_form = InterfaceLinkForm(instance=interface, data=params)
                if link_form.is_valid():
                    link_form.save()
                else:
                    raise ValidationError(link_form.errors)
            elif subnet_id is not None:
                link_ip = interface.ip_addresses.get(
                    alloc_type=IPADDRESS_TYPE.STICKY, ip__isnull=True
                )
                link_ip.subnet = Subnet.objects.get(id=subnet_id)
                link_ip.save()

    def create_physical(self, params):
        """Create physical interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = PhysicalInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._update_obj_tags(interface, params)
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def create_vlan(self, params):
        """Create VLAN interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        params["parents"] = [params.pop("parent")]
        form = VLANInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._update_obj_tags(interface, params)
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def create_bond(self, params):
        """Create bond interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        form = BondInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._update_obj_tags(interface, params)
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def create_bridge(self, params):
        """Create bridge interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        if node.status == NODE_STATUS.ALLOCATED:
            form = AcquiredBridgeInterfaceForm(node=node, data=params)
        else:
            form = BridgeInterfaceForm(node=node, data=params)
        if form.is_valid():
            interface = form.save()
            self._update_obj_tags(interface, params)
            self._create_link_on_interface(interface, params)
        else:
            raise ValidationError(form.errors)

    def update_interface(self, params):
        """Update the interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        if node.status == NODE_STATUS.DEPLOYED:
            interface_form = DeployedInterfaceForm
        else:
            interface_form = InterfaceForm.get_interface_form(interface.type)
        form = interface_form(instance=interface, data=params)
        if form.is_valid():
            interface = form.save()
            self._update_obj_tags(interface, params)
        else:
            raise ValidationError(form.errors)
        if "mode" in params:
            self.link_subnet(params)
        return self.full_dehydrate(node)

    def delete_interface(self, params):
        """Delete the interface."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        interface.delete()

    def link_subnet(self, params):
        """Create or update the link."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        subnet = None
        if "subnet" in params:
            subnet = Subnet.objects.get(id=params["subnet"])
        if "link_id" in params:
            if interface.ip_addresses.filter(id=params["link_id"]).exists():
                # We are updating an already existing link.  Which may have
                # been deleted.
                interface.update_link_by_id(
                    params["link_id"],
                    params["mode"],
                    subnet,
                    ip_address=params.get("ip_address", None),
                )
        else:
            # We are creating a new link.
            interface.link_subnet(
                params["mode"],
                subnet,
                ip_address=params.get("ip_address", None),
            )

    def unlink_subnet(self, params):
        """Delete the link."""
        node = self._get_node_or_permission_error(
            params, permission=self._meta.edit_permission
        )
        interface = Interface.objects.get(node=node, id=params["interface_id"])
        interface.unlink_subnet_by_id(params["link_id"])

    @asynchronous(timeout=45)
    def check_power(self, params):
        """Check the power state of the node."""

        def eb_unknown(failure):
            failure.trap(UnknownPowerType, NotImplementedError)
            return POWER_STATE.UNKNOWN

        def eb_error(failure):
            log.err(failure, "Failed to update power state of machine.")
            return POWER_STATE.ERROR

        @transactional
        def update_state(state):
            if state in [POWER_STATE.ERROR, POWER_STATE.UNKNOWN]:
                # Update the power state only if it was an error or unknown as
                # that could have come from the previous errbacks.
                obj = self.get_object(params)
                obj.update_power_state(state)
            return state

        d = deferToDatabase(transactional(self.get_object), params)
        d.addCallback(lambda node: node.power_query())
        d.addErrback(eb_unknown)
        d.addErrback(eb_error)
        d.addCallback(partial(deferToDatabase, update_state))
        return d

    def _get_node_or_permission_error(self, params, permission=None):
        node = self.get_object(params, permission=permission)
        if node.locked:
            raise HandlerPermissionError()
        return node
