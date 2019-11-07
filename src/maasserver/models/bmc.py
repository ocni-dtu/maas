# Copyright 2015-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""BMC objects."""

__all__ = ["BMC"]

from functools import partial
import re

from django.contrib.postgres.fields import ArrayField, JSONField
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import MinValueValidator
from django.db import transaction
from django.db.models import (
    BigIntegerField,
    BooleanField,
    CASCADE,
    CharField,
    FloatField,
    ForeignKey,
    IntegerField,
    Manager,
    ManyToManyField,
    PROTECT,
    SET_DEFAULT,
    SET_NULL,
    TextField,
)
from django.db.models.query import QuerySet
from django.shortcuts import get_object_or_404
from maasserver import DefaultMeta
from maasserver.clusterrpc.pods import decompose_machine
from maasserver.enum import (
    BMC_TYPE,
    BMC_TYPE_CHOICES,
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
    NODE_CREATION_TYPE,
    NODE_STATUS,
)
from maasserver.exceptions import PodProblem
from maasserver.models.blockdevice import BlockDevice
from maasserver.models.cleansave import CleanSave
from maasserver.models.fabric import Fabric
from maasserver.models.interface import PhysicalInterface
from maasserver.models.iscsiblockdevice import (
    get_iscsi_target,
    ISCSIBlockDevice,
)
from maasserver.models.node import get_default_zone, Machine, Node
from maasserver.models.physicalblockdevice import PhysicalBlockDevice
from maasserver.models.podhints import PodHints
from maasserver.models.podstoragepool import PodStoragePool
from maasserver.models.resourcepool import ResourcePool
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.models.subnet import Subnet
from maasserver.models.tag import Tag
from maasserver.models.timestampedmodel import TimestampedModel
from maasserver.models.vlan import VLAN
from maasserver.models.zone import Zone
from maasserver.permissions import PodPermission
from maasserver.rpc import getAllClients, getClientFromIdentifiers
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
import petname
from provisioningserver.drivers import SETTING_SCOPE
from provisioningserver.drivers.pod import BlockDeviceType, InterfaceAttachType
from provisioningserver.drivers.power.registry import PowerDriverRegistry
from provisioningserver.enum import MACVLAN_MODE_CHOICES
from provisioningserver.logger import get_maas_logger
from provisioningserver.utils.constraints import LabeledConstraintMap
from provisioningserver.utils.network import get_ifname_for_label
from provisioningserver.utils.twisted import asynchronous
from twisted.internet.defer import inlineCallbacks


maaslog = get_maas_logger("node")
podlog = get_maas_logger("pod")


def get_requested_ips(requested_machine):
    """Creates a map of requested IP addresses, given a RequestedMachine."""
    if requested_machine is not None:
        requested_ips = {
            interface.ifname: interface.requested_ips
            for interface in requested_machine.interfaces
            if (
                interface.ifname is not None
                and len(interface.requested_ips) > 0
            )
        }
    else:
        requested_ips = {}
    return requested_ips


def get_ip_modes(requested_machine):
    """Creates a map of requested IP modes, given a RequestedMachine."""
    if requested_machine is not None:
        ip_modes = {
            interface.ifname: interface.ip_mode
            for interface in requested_machine.interfaces
            if (interface.ifname is not None and interface.ip_mode is not None)
        }
    else:
        ip_modes = {}
    return ip_modes


class BaseBMCManager(Manager):
    """A utility to manage the collection of BMCs."""

    extra_filters = {}

    def get_queryset(self):
        queryset = QuerySet(self.model, using=self._db)
        return queryset.filter(**self.extra_filters)


class BMCManager(BaseBMCManager):
    """Manager for `BMC` not `Pod`'s."""

    extra_filters = {"bmc_type": BMC_TYPE.BMC}


class BMC(CleanSave, TimestampedModel):
    """A `BMC` represents an existing 'baseboard management controller'.  For
    practical purposes in MAAS, this is any addressable device that can control
    the power state of Nodes. The BMC associated with a Node is the one
    expected to control its power.

    Power parameters that apply to all nodes controlled by a BMC are stored
    here in the BMC. Those that are specific to different Nodes on the same BMC
    are stored in the Node model instances.

    :ivar ip_address: This `BMC`'s IP Address.
    :ivar power_type: The power type defines which type of BMC this is.
        Its value must match a power driver class name.
    :ivar power_parameters: Some JSON containing arbitrary parameters this
        BMC's power driver requires to function.
    :ivar objects: The :class:`BMCManager`.
    """

    objects = Manager()

    bmcs = BMCManager()

    bmc_type = IntegerField(
        choices=BMC_TYPE_CHOICES, editable=False, default=BMC_TYPE.DEFAULT
    )

    ip_address = ForeignKey(
        StaticIPAddress,
        default=None,
        blank=True,
        null=True,
        editable=False,
        on_delete=SET_NULL,
    )

    # The possible choices for this field depend on the power types advertised
    # by the rack controllers.  This needs to be populated on the fly, in
    # forms.py, each time the form to edit a node is instantiated.
    power_type = CharField(max_length=10, null=False, blank=True, default="")

    # JSON-encoded set of parameters for power control, limited to 32kiB when
    # encoded as JSON. These apply to all Nodes controlled by this BMC.
    power_parameters = JSONField(max_length=(2 ** 15), blank=True, default="")

    # Rack controllers that have access to the BMC by routing instead of
    # having direct layer 2 access.
    routable_rack_controllers = ManyToManyField(
        "RackController",
        blank=True,
        editable=True,
        through="BMCRoutableRackControllerRelationship",
        related_name="routable_bmcs",
    )

    # Name of the pod.
    name = CharField(max_length=255, default="", blank=True, unique=True)

    # Architectures this pod supports.
    architectures = ArrayField(
        TextField(), blank=True, null=True, default=list
    )

    # Pod capabilities.
    capabilities = ArrayField(TextField(), blank=True, null=True, default=list)

    # Number of cores in the pod.
    cores = IntegerField(blank=False, null=False, default=0)

    # Fastest CPU in the pod (MHz).
    cpu_speed = IntegerField(blank=False, null=False, default=0)

    # Total memory in the pod (XXX: units?).
    memory = IntegerField(blank=False, null=False, default=0)

    # Total storage available in the pod (bytes).
    local_storage = BigIntegerField(blank=False, null=False, default=0)

    # Number of disks in the pod (if applicable, otherwise set to -1).
    local_disks = IntegerField(blank=False, null=False, default=-1)

    # Total iSCSI storage available in the pod (if applicable, otherwise -1).
    iscsi_storage = BigIntegerField(  # Bytes
        blank=False, null=False, default=-1
    )

    # Resource pool for this pod.
    pool = ForeignKey(
        ResourcePool,
        default=None,
        null=True,
        blank=True,
        editable=True,
        on_delete=PROTECT,
    )

    # Physical zone this pod is in.
    zone = ForeignKey(
        Zone,
        verbose_name="Physical zone",
        default=get_default_zone,
        editable=True,
        db_index=True,
        on_delete=SET_DEFAULT,
    )

    # Tags for this pod.
    tags = ArrayField(TextField(), blank=True, null=True, default=list)

    # CPU over-commit ratio.
    cpu_over_commit_ratio = FloatField(
        default=1, validators=[MinValueValidator(0)]
    )

    # Memory over-commit ratio.
    memory_over_commit_ratio = FloatField(
        default=1, validators=[MinValueValidator(0)]
    )

    # Default storage pool for the pod.
    default_storage_pool = ForeignKey(
        PodStoragePool,
        null=True,
        blank=True,
        related_name="+",
        on_delete=SET_NULL,
    )

    # Default MACVLAN mode for the pod.
    # This is used as the default macvlan mode when a user wants
    # to create a macvlan interface for a VM.
    default_macvlan_mode = CharField(
        max_length=32,
        null=True,
        blank=True,
        default=None,
        choices=MACVLAN_MODE_CHOICES,
    )

    def __str__(self):
        return "%s (%s)" % (
            self.id,
            self.ip_address if self.ip_address else "No IP",
        )

    def _as(self, model):
        """Create a `model` that shares underlying storage with `self`.

        In other words, the newly returned object will be an instance of
        `model` and its `__dict__` will be `self.__dict__`. Not a copy, but a
        reference to, so that changes to one will be reflected in the other.
        """
        new = object.__new__(model)
        new.__dict__ = self.__dict__
        return new

    def as_bmc(self):
        """Return a reference to self that behaves as a `BMC`."""
        return self._as(BMC)

    def as_pod(self):
        """Return a reference to self that behaves as a `Pod`."""
        return self._as(Pod)

    _as_self = {BMC_TYPE.BMC: as_bmc, BMC_TYPE.POD: as_pod}

    def as_self(self):
        """Return a reference to self that behaves as its own type."""
        return self._as_self[self.bmc_type](self)

    def delete(self):
        """Delete this BMC."""
        maaslog.info("%s: Deleting BMC", self)
        super(BMC, self).delete()

    def save(self, *args, **kwargs):
        """Save this BMC."""
        super(BMC, self).save(*args, **kwargs)
        # We let name be blank for the initial save, but fix it before the
        # save completes.  This is because set_random_name() operates by
        # trying to re-save the BMC with a random hostname, and retrying until
        # there is no conflict.
        if self.name == "":
            self.set_random_name()

    def set_random_name(self):
        """Set a random `name`."""
        while True:
            self.name = petname.Generate(2, "-")
            try:
                self.save()
            except ValidationError:
                pass
            else:
                break

    def clean(self):
        """ Update our ip_address if the address extracted from our power
        parameters has changed. """
        new_ip = BMC.extract_ip_address(self.power_type, self.power_parameters)
        current_ip = None if self.ip_address is None else self.ip_address.ip
        # Set the ip_address field.  If we have a bracketed address, assume
        # it's IPv6, and strip the brackets.
        if new_ip and new_ip.startswith("[") and new_ip.endswith("]"):
            new_ip = new_ip[1:-1]
        if new_ip != current_ip:
            if not new_ip:
                self.ip_address = None
            else:
                # Update or create a StaticIPAddress for the new IP.
                try:
                    # This atomic block ensures that an exception within will
                    # roll back only this block's DB changes. This allows us to
                    # swallow exceptions in here and keep all changes made
                    # before or after this block is executed.
                    with transaction.atomic():
                        subnet = Subnet.objects.get_best_subnet_for_ip(new_ip)
                        (self.ip_address, _) = StaticIPAddress.objects.exclude(
                            alloc_type=IPADDRESS_TYPE.DISCOVERED
                        ).get_or_create(
                            ip=new_ip,
                            defaults={
                                "alloc_type": IPADDRESS_TYPE.STICKY,
                                "subnet": subnet,
                            },
                        )
                except Exception as error:
                    maaslog.info(
                        "BMC could not save extracted IP "
                        "address '%s': '%s'",
                        new_ip,
                        error,
                    )
                    raise error

    @staticmethod
    def scope_power_parameters(power_type, power_params):
        """Separate the global, bmc related power_parameters from the local,
        node-specific ones."""
        if not power_type:
            # If there is no power type, treat all params as node params.
            return (False, {}, power_params)
        power_driver = PowerDriverRegistry.get_item(power_type)
        if power_driver is None:
            # If there is no power driver, treat all params as node params.
            return (False, {}, power_params)
        power_fields = power_driver.settings
        if not power_fields:
            # If there is no parameter info, treat all params as node params.
            return (False, {}, power_params)
        bmc_params = {}
        node_params = {}
        for param_name in power_params:
            power_field = power_driver.get_setting(param_name)
            if power_field and power_field.get("scope") == SETTING_SCOPE.BMC:
                bmc_params[param_name] = power_params[param_name]
            else:
                node_params[param_name] = power_params[param_name]
        return (power_driver.chassis, bmc_params, node_params)

    @staticmethod
    def extract_ip_address(power_type, power_parameters):
        """ Extract the ip_address from the power_parameters. If there is no
        power_type, no power_parameters, or no valid value provided in the
        power_address field, returns None. """
        if not power_type or not power_parameters:
            # Nothing to extract.
            return None
        power_driver = PowerDriverRegistry.get_item(power_type)
        if power_driver is None:
            maaslog.warning("No power driver for power type %s" % power_type)
            return None
        power_type_parameters = power_driver.settings
        if not power_type_parameters:
            maaslog.warning(
                "No power driver settings for power type %s" % power_type
            )
            return None
        ip_extractor = power_driver.ip_extractor
        if not ip_extractor:
            maaslog.info(
                "No IP extractor configured for power type %s. "
                "IP will not be extracted." % power_type
            )
            return None
        field_value = power_parameters.get(ip_extractor.get("field_name"))
        if not field_value:
            maaslog.warning(
                "IP extractor field_value missing for %s" % power_type
            )
            return None
        extraction_pattern = ip_extractor.get("pattern")
        if not extraction_pattern:
            maaslog.warning(
                "IP extractor extraction_pattern missing for %s" % power_type
            )
            return None
        match = re.match(extraction_pattern, field_value)
        if match:
            return match.group("address")
        # no match found - return None
        return None

    def get_layer2_usable_rack_controllers(self, with_connection=True):
        """Return a list of `RackController`'s that have the ability to access
        this `BMC` directly through a layer 2 connection."""
        ip_address = self.ip_address
        if ip_address is None or ip_address.ip is None or ip_address.ip == "":
            return set()

        # The BMC has a valid StaticIPAddress set. Make sure that the subnet
        # is correct for that BMC.
        subnet = Subnet.objects.get_best_subnet_for_ip(ip_address.ip)
        if subnet is not None and self.ip_address.subnet_id != subnet.id:
            self.ip_address.subnet = subnet
            self.ip_address.save()

        # Circular imports.
        from maasserver.models.node import RackController

        return RackController.objects.filter_by_url_accessible(
            ip_address.ip, with_connection=with_connection
        )

    def get_routable_usable_rack_controllers(self, with_connection=True):
        """Return a list of `RackController`'s that have the ability to access
        this `BMC` through a route on the rack controller."""
        routable_racks = [
            relationship.rack_controller
            for relationship in (
                self.routable_rack_relationships.all().select_related(
                    "rack_controller"
                )
            )
            if relationship.routable
        ]
        if with_connection:
            conn_rack_ids = [client.ident for client in getAllClients()]
            return [
                rack
                for rack in routable_racks
                if rack.system_id in conn_rack_ids
            ]
        else:
            return routable_racks

    def get_usable_rack_controllers(self, with_connection=True):
        """Return a list of `RackController`'s that have the ability to access
        this `BMC` either using layer2 or routable if no layer2 are available.
        """
        racks = self.get_layer2_usable_rack_controllers(
            with_connection=with_connection
        )
        if len(racks) == 0:
            # No layer2 routable rack controllers. Use routable rack
            # controllers.
            racks = self.get_routable_usable_rack_controllers(
                with_connection=with_connection
            )
        return racks

    def get_client_identifiers(self):
        """Return a list of identifiers that can be used to get the
        `rpc.common.Client` for this `BMC`.

        :raise NoBMCAccessError: Raised when no rack controllers have access
            to this `BMC`.
        """
        rack_controllers = self.get_usable_rack_controllers()
        identifers = [controller.system_id for controller in rack_controllers]
        return identifers

    def is_accessible(self):
        """If the BMC is accessible by at least one rack controller."""
        racks = self.get_usable_rack_controllers(with_connection=False)
        return len(racks) > 0

    def update_routable_racks(
        self, routable_racks_ids, non_routable_racks_ids
    ):
        """Set the `routable_rack_controllers` relationship to the new
        information."""
        BMCRoutableRackControllerRelationship.objects.filter(
            bmc=self.as_bmc()
        ).delete()
        self._create_racks_relationship(routable_racks_ids, True)
        self._create_racks_relationship(non_routable_racks_ids, False)

    def _create_racks_relationship(self, rack_ids, routable):
        """Create `BMCRoutableRackControllerRelationship` for list of
        `rack_ids` and wether they are `routable`."""
        # Circular imports.
        from maasserver.models.node import RackController

        for rack_id in rack_ids:
            try:
                rack = RackController.objects.get(system_id=rack_id)
            except RackController.DoesNotExist:
                # Possible it was delete before this call, but very very rare.
                pass
            BMCRoutableRackControllerRelationship(
                bmc=self, rack_controller=rack, routable=routable
            ).save()


class PodManager(BaseBMCManager):
    """Manager for `Pod` not `BMC`'s."""

    extra_filters = {"bmc_type": BMC_TYPE.POD}

    def get_pods(self, user, perm):
        """Fetch `ResourcePool`'s on which the User_ has the given permission.

        :param user: The user that should be used in the permission check.
        :type user: User_
        :param perm: Type of access requested.
        :type perm: `PodPermission`

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User

        """
        # Circular imports.
        from maasserver.rbac import rbac

        if rbac.is_enabled():
            if perm == PodPermission.view:
                fetched = rbac.get_resource_pool_ids(
                    user.username, "view", "view-all"
                )
                pool_ids = set(fetched["view"] + fetched["view-all"])
                return self.filter(pool_id__in=pool_ids)
            elif perm == PodPermission.edit or perm == PodPermission.compose:
                return self.filter(
                    pool_id__in=rbac.get_resource_pool_ids(
                        user.username, "admin-machines"
                    )["admin-machines"]
                )
            elif perm == PodPermission.dynamic_compose:
                fetched = rbac.get_resource_pool_ids(
                    user.username, "deploy-machines", "admin-machines"
                )
                pool_ids = set(
                    fetched["deploy-machines"] + fetched["admin-machines"]
                )
                return self.filter(pool_id__in=pool_ids)
            else:
                raise ValueError("Unknown perm: %s", perm)
        return self.all()

    def get_pod_or_404(self, id, user, perm, **kwargs):
        """Fetch a `Pod` by id.  Raise exceptions if no `Pod` with
        this system_id exist or if the provided user has not the required
        permission on this `Pod`.

        :param id: The id.
        :type id: int
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param perm: The permission to assert that the user has on the node.
        :type perm: `PodPermission`
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        kwargs.update(self.extra_filters)
        pod = get_object_or_404(self.model, id=id, **kwargs)
        if user.has_perm(perm, pod):
            return pod
        else:
            raise PermissionDenied()

    def have_rsd(self, user, perm):
        """Are there any RSD hosts?

        :param user: The user that should be used in the permission check.
        :type user: User_
        :param perm: Type of access requested.
        :type perm: `PodPermission`

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User
        """
        return self.get_pods(user, perm).filter(power_type="rsd").exists()


class Pod(BMC):
    """A `Pod` represents a `BMC` that controls multiple machines."""

    class Meta(DefaultMeta):
        proxy = True

    objects = PodManager()

    _machine_name_re = re.compile(r"[a-z][a-z0-9-]+$", flags=re.I)

    def __init__(self, *args, **kwargs):
        if "pool" not in kwargs:
            kwargs["pool"] = ResourcePool.objects.get_default_resource_pool()
        super(Pod, self).__init__(bmc_type=BMC_TYPE.POD, *args, **kwargs)

    def clean(self):
        super().clean()
        if self.pool is None:
            raise ValidationError("A pod needs to have a pool")

    @property
    def host(self):
        if self.ip_address is not None:
            interface = self.ip_address.get_interface()
            if interface is not None:
                return interface.node
        return None

    def sync_hints(self, discovered_hints):
        """Sync the hints with `discovered_hints`."""
        try:
            hints = self.hints
        except PodHints.DoesNotExist:
            hints = self.hints = PodHints()
        hints.cores = discovered_hints.cores
        hints.cpu_speed = discovered_hints.cpu_speed
        hints.memory = discovered_hints.memory
        hints.local_storage = discovered_hints.local_storage
        hints.local_disks = discovered_hints.local_disks
        hints.iscsi_storage = discovered_hints.iscsi_storage
        hints.save()

    def add_tag(self, tag):
        """Add tag to Pod."""
        if tag not in self.tags:
            self.tags = self.tags + [tag]

    def remove_tag(self, tag):
        """Remove tag from Pod."""
        if tag in self.tags:
            tags = self.tags.copy()
            tags.remove(tag)
            self.tags = tags

    def check_over_commit_ratios(self, requested_cores, requested_memory):
        """Checks that requested cpu cores and memory are within the
        currently available resources capped by the overcommit ratios."""
        message = ""
        used_cores = self.get_used_cores()
        used_memory = self.get_used_memory()
        over_commit_cores = self.cores * self.cpu_over_commit_ratio
        potential_cores = used_cores + requested_cores
        over_commit_memory = self.memory * self.memory_over_commit_ratio
        potential_memory = used_memory + requested_memory
        if (over_commit_cores - potential_cores) < 0:
            message = (
                "CPU overcommit ratio is %s and there are %s "
                "available resources; %s requested."
                % (
                    self.cpu_over_commit_ratio,
                    (self.cores - used_cores),
                    requested_cores,
                )
            )
        if (over_commit_memory - potential_memory) < 0:
            message += (
                "Memory overcommit ratio is %s and there are %s "
                "available resources; %s requested."
                % (
                    self.memory_over_commit_ratio,
                    (self.memory - used_memory),
                    requested_memory,
                )
            )
        return message

    def _find_existing_machine(self, discovered_machine, mac_machine_map):
        """Find a `Machine` in `mac_machine_map` based on the interface MAC
        addresses from `discovered_machine`."""
        for interface in discovered_machine.interfaces:
            if interface.mac_address in mac_machine_map:
                return mac_machine_map[interface.mac_address]
        return None

    def _get_storage_pool_by_id(self, pool_id):
        """Get the `PodStoragePool` base on the `pool_id`."""
        # Finding storage pool in python instead of using the database, so
        # preloaded data is used. This prevents un-needed database queries.
        for pool in self.storage_pools.all():
            if pool.pool_id == pool_id:
                return pool

    def _create_physical_block_device(self, discovered_bd, machine, name=None):
        """Create's a new `PhysicalBlockDevice` for `machine`."""
        if name is None:
            name = machine.get_next_block_device_name()
        model = discovered_bd.model
        serial = discovered_bd.serial
        if model is None:
            model = ""
        if serial is None:
            serial = ""
        storage_pool = None
        if discovered_bd.storage_pool:
            storage_pool = self._get_storage_pool_by_id(
                discovered_bd.storage_pool
            )
        return PhysicalBlockDevice.objects.create(
            numa_node=machine.default_numanode,
            name=name,
            id_path=discovered_bd.id_path,
            model=model,
            serial=serial,
            size=discovered_bd.size,
            block_size=discovered_bd.block_size,
            tags=discovered_bd.tags,
            storage_pool=storage_pool,
        )

    def _create_iscsi_block_device(self, discovered_bd, machine, name=None):
        """Create's a new `ISCSIBlockDevice` for `machine`.

        `ISCSIBlockDevice.target` are unique. So if one exists on another
        machine it will be moved from that machine to this machine.
        """
        if name is None:
            name = machine.get_next_block_device_name()
        target = get_iscsi_target(discovered_bd.iscsi_target)
        block_device, created = ISCSIBlockDevice.objects.get_or_create(
            target=target,
            defaults={
                "name": name,
                "node": machine,
                "size": discovered_bd.size,
                "block_size": discovered_bd.block_size,
                "tags": discovered_bd.tags,
            },
        )
        if not created:
            podlog.warning(
                "%s: ISCSI block device with target %s was discovered on "
                "machine %s and was moved from %s."
                % (
                    self.name,
                    target,
                    machine.hostname,
                    block_device.node.hostname,
                )
            )
            block_device.name = name
            block_device.node = machine
            block_device.size = discovered_bd.size
            block_device.block_size = discovered_bd.block_size
            block_device.tags = discovered_bd.tags
            block_device.save()
        return block_device

    def _create_interface(self, discovered_nic, machine, name=None):
        """Create's a new physical `Interface` for `machine`."""
        # XXX blake_r 2017-03-09: At the moment just connect the boot interface
        # to the VLAN where DHCP is running, unless none is running then
        # connect it to the default VLAN. All remaining interfaces will stay
        # disconnected.
        vlan = None
        if discovered_nic.boot:
            vlan = VLAN.objects.filter(dhcp_on=True).order_by("id").first()
            if vlan is None:
                vlan = Fabric.objects.get_default_fabric().get_default_vlan()
        if name is None:
            name = machine.get_next_ifname()
        nic, created = PhysicalInterface.objects.get_or_create(
            mac_address=discovered_nic.mac_address,
            defaults={
                "name": name,
                "node": machine,
                "numa_node": machine.default_numanode,
                "tags": discovered_nic.tags,
                "vlan": vlan,
            },
        )
        if not created:
            podlog.warning(
                "%s: interface with MAC address %s was discovered on "
                "machine %s and was moved from %s."
                % (
                    self.name,
                    discovered_nic.mac_address,
                    machine.hostname,
                    nic.node.hostname,
                )
            )
            nic.name = name
            nic.node = machine
            nic.tags = discovered_nic.tags
            nic.vlan = vlan
            nic.ip_addresses.all().delete()
            nic.save()
        return nic

    def create_machine(
        self,
        discovered_machine,
        commissioning_user,
        skip_commissioning=False,
        creation_type=NODE_CREATION_TYPE.PRE_EXISTING,
        interfaces=None,
        requested_machine=None,
        **kwargs
    ):
        """Create's a `Machine` from `discovered_machines` for this pod."""
        if skip_commissioning:
            status = NODE_STATUS.READY
        else:
            status = NODE_STATUS.NEW

        # Check to see if discovered machine's hostname is legal and unique.
        if discovered_machine.hostname:
            if Node.objects.filter(
                hostname=discovered_machine.hostname
            ).exists():
                discovered_machine.hostname = None
            elif not self._machine_name_re.match(discovered_machine.hostname):
                discovered_machine.hostname = None

        # Set the zone for the machine.
        # This allows machines to be created in the Pod
        # with a zone other than the zone of the Pod.
        zone = kwargs.pop("zone", None)
        if zone is None:
            zone = self.zone

        pool = kwargs.pop("pool", None)
        if pool is None:
            pool = self.pool

        if interfaces is not None:
            assert isinstance(interfaces, LabeledConstraintMap)

        requested_ips = get_requested_ips(requested_machine)
        ip_modes = get_ip_modes(requested_machine)

        # Create the machine.
        machine = Machine(
            hostname=discovered_machine.hostname,
            architecture=discovered_machine.architecture,
            status=status,
            cpu_count=discovered_machine.cores,
            cpu_speed=discovered_machine.cpu_speed,
            memory=discovered_machine.memory,
            power_state=discovered_machine.power_state,
            creation_type=creation_type,
            pool=pool,
            zone=zone,
            **kwargs
        )
        machine.bmc = self
        machine.instance_power_parameters = discovered_machine.power_parameters
        if not machine.hostname:
            machine.set_random_hostname()
        machine.save()

        self._assign_tags(machine, discovered_machine)
        self._assign_storage(machine, discovered_machine, skip_commissioning)
        created_interfaces = self._assign_interfaces(
            machine, discovered_machine, interfaces, skip_commissioning
        )
        self._assign_ip_addresses(
            discovered_machine, created_interfaces, requested_ips, ip_modes
        )

        # New machines get commission started immediately unless skipped.
        if not skip_commissioning:
            skip_networking = False
            # If an interfaces constraint was specified, don't reset the
            # networking parameters. (Instead, allow them to be set based on
            # what was requested in the constraints string.)
            if interfaces is not None and len(interfaces) > 0:
                skip_networking = True
            machine.start_commissioning(
                commissioning_user, skip_networking=skip_networking
            )

        return machine

    def _assign_ip_addresses(
        self, discovered_machine, created_interfaces, allocated_ips, ip_modes
    ):
        # We need a second pass here to configure interfaces that the above
        # function call would otherwise change.
        if self.host is not None:
            self._update_vlans_based_on_pod_host(
                created_interfaces, discovered_machine
            )
        # Allocate any IP addresses the user requested.
        for interface in created_interfaces:
            if interface.name in allocated_ips:
                # Replace any pre-configured addresses with what the user
                # has requested.
                interface.ip_addresses.clear()
                for address in allocated_ips[interface.name]:
                    ip_address = StaticIPAddress.objects.allocate_new(
                        requested_address=address
                    )
                    # The VLAN of the interface might be inconsistent with the
                    # subnet's VLAN, if the pod doesn't have a host and MAAS
                    # guessed incorrectly in an earlier step. So trust the
                    # user's input here.
                    if interface.vlan != ip_address.subnet.vlan:
                        interface.vlan = ip_address.subnet.vlan
                        interface.save()
                    ip_address.save()
                    interface.ip_addresses.add(ip_address)
            if interface.name in ip_modes:
                mode = ip_modes[interface.name]
                if mode == "unconfigured":
                    for address in interface.ip_addresses.all():
                        # User requested an unconfigured interface; change
                        # the AUTO that was created to a STICKY and ensure
                        # the IP address is cleared out.
                        address.alloc_type = IPADDRESS_TYPE.STICKY
                        address.ip = None
                        address.save()

    def _assign_interfaces(
        self,
        machine,
        discovered_machine,
        interface_constraints,
        skip_commissioning,
    ):
        # Enumerating the LabeledConstraintMap of interfaces will yield the
        # name of each interface, in the same order that they will exist
        # on the hypervisor. (This is a fortunate coincidence, since
        # dictionaries in Python 3.6+ preserve insertion order.)
        if interface_constraints is not None:
            interface_names = [
                get_ifname_for_label(label) for label in interface_constraints
            ]
        else:
            interface_names = []
        if len(discovered_machine.interfaces) > len(interface_names):
            # The lists should never have different lengths, but use default
            # names for all interfaces to avoid conflicts, just in case.
            # (This also happens if no interface labels were supplied.)
            interface_names = [
                "eth%d" % i for i in range(len(discovered_machine.interfaces))
            ]
        # Create the discovered interface and set the default networking
        # configuration.
        created_interfaces = []
        for idx, discovered_nic in enumerate(discovered_machine.interfaces):
            interface = self._create_interface(
                discovered_nic, machine, name=interface_names[idx]
            )
            created_interfaces.append(interface)
            if discovered_nic.boot:
                machine.boot_interface = interface
                machine.save(update_fields=["boot_interface"])
        if skip_commissioning:
            machine.set_initial_networking_configuration()
        return created_interfaces

    def _assign_storage(self, machine, discovered_machine, skip_commissioning):
        # Create the discovered block devices and set the initial storage
        # layout for the machine.
        for idx, discovered_bd in enumerate(discovered_machine.block_devices):
            if discovered_bd.type == BlockDeviceType.PHYSICAL:
                try:
                    self._create_physical_block_device(
                        discovered_bd,
                        machine,
                        name=BlockDevice._get_block_name_from_idx(idx),
                    )
                except Exception:
                    if skip_commissioning:
                        # Commissioning is not being performed for this
                        # machine. When not performing commissioning it is
                        # required for all physical block devices be created,
                        # otherwise this is allowed to fail as commissioning
                        # will discover this information.
                        raise
            elif discovered_bd.type == BlockDeviceType.ISCSI:
                # iSCSI block devices cannot fail, they must provide the
                # required information.
                self._create_iscsi_block_device(
                    discovered_bd,
                    machine,
                    name=BlockDevice._get_block_name_from_idx(idx),
                )
            else:
                raise ValueError(
                    "Unknown block device type: %s" % discovered_bd.type
                )
        if skip_commissioning:
            machine.set_default_storage_layout()

    def _assign_tags(self, machine, discovered_machine):
        # Assign the discovered tags.
        for discovered_tag in discovered_machine.tags:
            tag, _ = Tag.objects.get_or_create(name=discovered_tag)
            machine.tags.add(tag)
        # Assign the Pod's tags.
        existing_tags = machine.tags.all().values("name")
        for pod_tag in self.tags:
            # Only if not a duplicate.
            if pod_tag not in existing_tags:
                tag, _ = Tag.objects.get_or_create(name=pod_tag)
                machine.tags.add(tag)

    def _update_vlans_based_on_pod_host(
        self, created_interfaces, discovered_machine
    ):
        """Matches up newly-created interfaces with interfaces on the pod.host,
        given a list of interfaces that were created for the machine, and
        the DiscoveredMachine object.
        """
        # Circular imports.
        from maasserver.models import Interface

        interfaces = {
            interface.name: interface
            for interface in Interface.objects.all_interfaces_parents_first(
                self.host
            )
        }
        for idx, discovered_nic in enumerate(discovered_machine.interfaces):
            if discovered_nic.attach_type in (
                InterfaceAttachType.BRIDGE,
                InterfaceAttachType.MACVLAN,
            ):
                host_attach_interface = interfaces.get(
                    discovered_nic.attach_name, None
                )
                if host_attach_interface is not None:
                    # If we get to this point, we found the interface the
                    # the VM has been attached to. Update the VLAN (but
                    # only if necessary).
                    host_vlan = host_attach_interface.vlan
                    interface = created_interfaces[idx]
                    if host_vlan != interface.vlan:
                        interface.vlan = host_vlan
                        interface.save()
                    if interface.ip_addresses.count() == 0:
                        interface.force_auto_or_dhcp_link()
                    continue

    def _sync_machine(self, discovered_machine, existing_machine):
        """Sync's the information from `discovered_machine` to update
        `existing_machine`."""
        # Log if the machine is moving under a pod or being moved from
        # a different pod.
        if existing_machine.bmc_id != self.id:
            if (
                existing_machine.bmc_id is None
                or existing_machine.bmc.bmc_type == BMC_TYPE.BMC
            ):
                podlog.warning(
                    "%s: %s has been moved under the pod, previously "
                    "it was not part of any pod."
                    % (self.name, existing_machine.hostname)
                )
            else:
                podlog.warning(
                    "%s: %s has been moved under the pod, previously "
                    "it was part of pod %s."
                    % (
                        self.name,
                        existing_machine.hostname,
                        existing_machine.bmc.name,
                    )
                )
            existing_machine.bmc = self

        # Sync power state and parameters for this machine always.
        existing_machine.power_state = discovered_machine.power_state
        existing_machine.instance_power_parameters = (
            discovered_machine.power_parameters
        )

        # If this machine is pre-existing or manually composed then we skip
        # syncing all the remaining information because MAAS commissioning
        # will discover this information. Any changes on the MAAS in the pod
        # for pre-existing and manual require the machine to be
        # re-commissioned.
        if existing_machine.creation_type in [
            NODE_CREATION_TYPE.PRE_EXISTING,
            NODE_CREATION_TYPE.MANUAL,
        ]:
            existing_machine.save()
            return

        # Sync machine instance values.
        # We are skipping hostname syncing so that any changes to the
        # hostname in MAAS are not overwritten.
        existing_machine.architecture = discovered_machine.architecture
        existing_machine.cpu_count = discovered_machine.cores
        existing_machine.cpu_speed = discovered_machine.cpu_speed
        existing_machine.memory = discovered_machine.memory
        existing_machine.save()

        # Sync the tags to make sure they match the discovered machine.
        add_tags = set(discovered_machine.tags)
        for existing_tag_inst in existing_machine.tags.all():
            if existing_tag_inst.name in add_tags:
                add_tags.remove(existing_tag_inst.name)
            else:
                existing_machine.tags.remove(existing_tag_inst)
        for tag in add_tags:
            tag, _ = Tag.objects.get_or_create(name=tag)
            existing_machine.tags.add(tag)

        # Sync the block devices and interfaces on the machine.
        self._sync_block_devices(
            discovered_machine.block_devices, existing_machine
        )
        self._sync_interfaces(discovered_machine.interfaces, existing_machine)

    def _sync_block_devices(self, block_devices, existing_machine):
        """Sync the `block_devices` to the `existing_machine`."""
        model_mapping = {
            "%s/%s" % (block_device.model, block_device.serial): block_device
            for block_device in block_devices
            if (
                block_device.type == BlockDeviceType.PHYSICAL
                and block_device.model
                and block_device.serial
            )
        }
        path_mapping = {
            block_device.id_path: block_device
            for block_device in block_devices
            if (
                block_device.type == BlockDeviceType.PHYSICAL
                and (not block_device.model or not block_device.serial)
            )
        }
        iscsi_mapping = {
            block_device.iscsi_target: block_device
            for block_device in block_devices
            if block_device.type == BlockDeviceType.ISCSI
        }
        existing_block_devices = map(
            lambda bd: bd.actual_instance,
            existing_machine.blockdevice_set.all(),
        )
        for block_device in existing_block_devices:
            if isinstance(block_device, PhysicalBlockDevice):
                if block_device.model and block_device.serial:
                    key = "%s/%s" % (block_device.model, block_device.serial)
                    if key in model_mapping:
                        self._sync_block_device(
                            model_mapping.pop(key), block_device
                        )
                    else:
                        block_device.delete()
                else:
                    if block_device.id_path in path_mapping:
                        self._sync_block_device(
                            path_mapping.pop(block_device.id_path),
                            block_device,
                        )
                    else:
                        block_device.delete()
            elif isinstance(block_device, ISCSIBlockDevice):
                target = get_iscsi_target(block_device.target)
                if target in iscsi_mapping:
                    self._sync_block_device(
                        iscsi_mapping.pop(target), block_device
                    )
                else:
                    block_device.delete()
        for _, discovered_block_device in model_mapping.items():
            self._create_physical_block_device(
                discovered_block_device, existing_machine
            )
        for _, discovered_block_device in path_mapping.items():
            self._create_physical_block_device(
                discovered_block_device, existing_machine
            )
        for _, discovered_block_device in iscsi_mapping.items():
            self._create_iscsi_block_device(
                discovered_block_device, existing_machine
            )

    def _sync_block_device(self, discovered_bd, existing_bd):
        """Sync the `discovered_bd` with the `existing_bd`.

        The model, serial, id_path, and target is not handled here because if
        either changed then no way of matching between an existing block
        device is possible.
        """
        existing_bd.size = discovered_bd.size
        existing_bd.block_size = discovered_bd.block_size
        existing_bd.tags = discovered_bd.tags

        # Update or remove the storage pool on physical block devices.
        if isinstance(existing_bd, PhysicalBlockDevice):
            if discovered_bd.storage_pool:
                existing_bd.storage_pool = self._get_storage_pool_by_id(
                    discovered_bd.storage_pool
                )
            elif existing_bd.storage_pool:
                existing_bd.storage_pool = None

        existing_bd.save()

    def _sync_interfaces(self, interfaces, existing_machine):
        """Sync the `interfaces` to the `existing_machine`."""
        mac_mapping = {nic.mac_address: nic for nic in interfaces}
        # interface_set has been preloaded so filtering is done locally.
        physical_interfaces = [
            nic
            for nic in existing_machine.interface_set.all()
            if nic.type == INTERFACE_TYPE.PHYSICAL
        ]
        for existing_nic in physical_interfaces:
            if existing_nic.mac_address in mac_mapping:
                discovered_nic = mac_mapping.pop(existing_nic.mac_address)
                self._sync_interface(discovered_nic, existing_nic)
                if discovered_nic.boot:
                    existing_machine.boot_interface = existing_nic
                    existing_machine.save(update_fields=["boot_interface"])
            else:
                existing_nic.delete()
        for _, discovered_nic in mac_mapping.items():
            interface = self._create_interface(
                discovered_nic, existing_machine
            )
            if discovered_nic.boot:
                existing_machine.boot_interface = interface
                existing_machine.save(update_fields=["boot_interface"])

    def _sync_interface(self, discovered_nic, existing_interface):
        """Sync the `discovered_nic` with the `existing_interface`.

        The MAC address is not handled here because if the MAC address has
        changed then no way of matching between an existing interface is
        possible.
        """
        # XXX blake_r 2016-12-20: At the moment only update the tags on the
        # interface. This needs to be improved to sync the connected VLAN. At
        # the moment we do not override what is set, allowing users to adjust
        # the VLAN if discovery is not identifying it correctly.
        existing_interface.tags = discovered_nic.tags
        existing_interface.save()

    def sync_machines(self, discovered_machines, commissioning_user):
        """Sync the machines on this pod from `discovered_machines`."""
        all_macs = [
            interface.mac_address
            for machine in discovered_machines
            for interface in machine.interfaces
        ]
        existing_machines = list(
            Node.objects.filter(interface__mac_address__in=all_macs)
            .prefetch_related("interface_set")
            .prefetch_related("blockdevice_set__physicalblockdevice")
            .prefetch_related("blockdevice_set__virtualblockdevice")
            .distinct()
        )
        machines = {
            machine.id: machine
            for machine in Node.objects.filter(bmc__id=self.id)
        }
        mac_machine_map = {
            interface.mac_address: machine
            for machine in existing_machines
            for interface in machine.interface_set.all()
        }
        for discovered_machine in discovered_machines:
            existing_machine = self._find_existing_machine(
                discovered_machine, mac_machine_map
            )
            if existing_machine is None:
                new_machine = self.create_machine(
                    discovered_machine, commissioning_user
                )
                podlog.info(
                    "%s: discovered new machine: %s"
                    % (self.name, new_machine.hostname)
                )
            else:
                self._sync_machine(discovered_machine, existing_machine)
                existing_machines.remove(existing_machine)
                machines.pop(existing_machine.id, None)
        for _, remove_machine in machines.items():
            remove_machine.delete()
            podlog.warning(
                "%s: machine %s no longer exists and was deleted."
                % (self.name, remove_machine.hostname)
            )

    def sync_storage_pools(self, discovered_storage_pools):
        """Sync the storage pools for the pod."""
        storage_pools_by_id = {
            pool.pool_id: pool for pool in self.storage_pools.all()
        }
        possible_default = None
        upgrade_default_pool = self.power_parameters.get(
            "default_storage_pool"
        )
        for discovered_pool in discovered_storage_pools:
            pool = storage_pools_by_id.pop(discovered_pool.id, None)
            if pool:
                pool.name = discovered_pool.name
                pool.pool_type = discovered_pool.type
                pool.path = discovered_pool.path
                pool.storage = discovered_pool.storage
                pool.save()
            else:
                pool = PodStoragePool.objects.create(
                    pod=self,
                    pool_id=discovered_pool.id,
                    name=discovered_pool.name,
                    pool_type=discovered_pool.type,
                    path=discovered_pool.path,
                    storage=discovered_pool.storage,
                )
                podlog.info(
                    "%s: discovered new storage pool: %s"
                    % (self.name, discovered_pool.name)
                )
            if possible_default is None:
                possible_default = pool
            if (
                upgrade_default_pool is not None
                and upgrade_default_pool == pool.name
            ):
                possible_default = pool
        if not self.default_storage_pool and possible_default:
            self.default_storage_pool = possible_default
            if upgrade_default_pool is not None:
                self.power_parameters = self.power_parameters.copy()
                self.power_parameters.pop("default_storage_pool", None)
            self.save()
        elif self.default_storage_pool in storage_pools_by_id.values():
            self.default_storage_pool = possible_default
            self.save()
        for _, pool in storage_pools_by_id.items():
            pool.delete()
            podlog.warning(
                "%s: storage pool %s no longer exists and was deleted."
                % (self.name, pool.name)
            )

    def sync(self, discovered_pod, commissioning_user):
        """Sync the pod and machines from the `discovered_pod`.

        This method ensures consistency with what is discovered by a pod
        driver and what is known to MAAS in the data model. Any machines,
        interfaces, and/or block devices that do not match the
        `discovered_pod` values will be removed.
        """
        self.architectures = discovered_pod.architectures
        self.capabilities = discovered_pod.capabilities
        self.cores = discovered_pod.cores
        self.cpu_speed = discovered_pod.cpu_speed
        self.memory = discovered_pod.memory
        self.local_storage = discovered_pod.local_storage
        self.local_disks = discovered_pod.local_disks
        self.iscsi_storage = discovered_pod.iscsi_storage
        self.tags = list(set(self.tags).union(discovered_pod.tags))
        self.save()
        self.sync_hints(discovered_pod.hints)
        self.sync_storage_pools(discovered_pod.storage_pools)
        self.sync_machines(discovered_pod.machines, commissioning_user)
        podlog.info("%s: finished syncing discovered information" % self.name)

    def get_used_cores(self, machines=None):
        """Get the number of used cores in the pod.

        :param machines: Deployed machines on this pod. Only used when
            the deployed machines have already been pulled from the database
            and no extra query needs to be performed.
        """
        if machines is None:
            machines = Machine.objects.filter(bmc__id=self.id)
        return sum(machine.cpu_count for machine in machines)

    def get_used_memory(self, machines=None):
        """Get the amount of used memory in the pod.

        :param machines: Deployed machines on this pod. Only used when
            the deployed machines have already been pulled from the database
            and no extra query needs to be performed.
        """
        if machines is None:
            machines = Machine.objects.filter(bmc__id=self.id)
        return sum(machine.memory for machine in machines)

    def get_used_local_storage(self, machines=None):
        """Get the amount of used local storage in the pod.

        :param machines: Deployed machines on this pod. Only used when
            the deployed machines have already been pulled from the database
            and no extra query needs to be performed.
        """
        if machines is None:
            machines = (
                Machine.objects.filter(bmc__id=self.id)
                .prefetch_related("blockdevice_set__iscsiblockdevice")
                .prefetch_related("blockdevice_set__virtualblockdevice")
                .prefetch_related("blockdevice_set__physicalblockdevice")
            )
        return sum(
            blockdevice.size
            for machine in machines
            for blockdevice in machine.blockdevice_set.all()
            if isinstance(blockdevice.actual_instance, PhysicalBlockDevice)
        )

    def get_used_local_disks(self, machines=None):
        """Get the amount of used local disks in the pod.

        :param machines: Deployed machines on this clusted. Only used when
            the deployed machines have already been pulled from the database
            and no extra query needs to be performed.
        """
        if machines is None:
            machines = (
                Machine.objects.filter(bmc__id=self.id)
                .prefetch_related("blockdevice_set__iscsiblockdevice")
                .prefetch_related("blockdevice_set__virtualblockdevice")
                .prefetch_related("blockdevice_set__physicalblockdevice")
            )
        return len(
            [
                blockdevice
                for machine in machines
                for blockdevice in machine.blockdevice_set.all()
                if isinstance(blockdevice.actual_instance, PhysicalBlockDevice)
            ]
        )

    def get_used_iscsi_storage(self, machines=None):
        """Get the amount of used iSCSI storage in the pod.

        :param machines: Deployed machines on this clusted. Only used when
            the deployed machines have already been pulled from the database
            and no extra query needs to be performed.
        """
        if machines is None:
            machines = (
                Machine.objects.filter(bmc__id=self.id)
                .prefetch_related("blockdevice_set__iscsiblockdevice")
                .prefetch_related("blockdevice_set__virtualblockdevice")
                .prefetch_related("blockdevice_set__physicalblockdevice")
            )
        return sum(
            blockdevice.size
            for machine in machines
            for blockdevice in machine.blockdevice_set.all()
            if isinstance(blockdevice.actual_instance, ISCSIBlockDevice)
        )

    def delete(self, *args, **kwargs):
        raise AttributeError(
            "Use `async_delete` instead. Deleting a Pod takes "
            "an asynchronous action."
        )

    def delete_and_wait(self):
        """Block the current thread while waiting for the pod to be deleted.

        This must not be called from a deferToDatabase thread; use the
        async_delete() method instead.
        """
        # Calculate the wait time based on the number of none pre-existing
        # machines. We allow maximum of 60 seconds per machine plus 60 seconds
        # for the pod.
        pod = self.as_pod()
        num_machines = Machine.objects.filter(bmc=pod)
        num_machines = num_machines.exclude(
            creation_type=NODE_CREATION_TYPE.PRE_EXISTING
        )
        pod.async_delete().wait((num_machines.count() * 60) + 60)

    @asynchronous
    def async_delete(self):
        """Delete a pod asynchronously.

        Any machine in the pod that needs to be decomposed will be decomposed
        before it is removed from the database.  If there are any errors during
        decomposition, the deletion of the machine and ultimately the pod are
        not stopped.
        """

        @transactional
        def gather_clients_and_machines(pod):
            decompose, pre_existing = [], []
            for machine in (
                Machine.objects.filter(bmc__id=pod.id)
                .order_by("id")
                .select_related("bmc")
            ):
                if machine.creation_type == NODE_CREATION_TYPE.PRE_EXISTING:
                    pre_existing.append(machine.id)
                else:
                    decompose.append((machine.id, machine.power_parameters))
            return (
                pod.id,
                pod.name,
                pod.power_type,
                pod.get_client_identifiers(),
                decompose,
                pre_existing,
            )

        @inlineCallbacks
        def decompose(result):
            (
                pod_id,
                pod_name,
                pod_type,
                client_idents,
                decompose,
                pre_existing,
            ) = result
            decomposed = []
            for machine_id, parameters in decompose:
                # Get a new client for every decompose because we might lose
                # a connection to a rack during this operation.
                client = yield getClientFromIdentifiers(client_idents)
                try:
                    yield decompose_machine(
                        client,
                        pod_type,
                        parameters,
                        pod_id=pod_id,
                        name=pod_name,
                    )
                except PodProblem:
                    # Catch all errors and continue.
                    break
                finally:
                    # Set the machine to decomposed regardless
                    # if it actually decomposed or not.
                    decomposed.append(machine_id)
            return pod_id, decomposed, pre_existing

        @transactional
        def perform_deletion(result):
            (pod_id, decomposed_ids, pre_existing_ids) = result
            pod = Pod.objects.get(id=pod_id)
            machines = Machine.objects.filter(id__in=decomposed_ids)
            for machine in machines:
                # Clear BMC (aka. this pod) so the signal handler does not
                # try to decompose it. Its already been decomposed.
                machine.bmc = None
                machine.delete()

            # Delete the pre-existing machines and finally the pod.
            for machine in Machine.objects.filter(id__in=pre_existing_ids):
                # We loop and call delete to ensure the `delete` method
                # on the machine object is actually called.
                machine.delete()
            # Call delete by bypassing the override that prevents its call.
            super(BMC, pod).delete()

        # Don't catch any errors here they are raised to the caller.
        d = deferToDatabase(gather_clients_and_machines, self)
        d.addCallback(decompose)
        d.addCallback(partial(deferToDatabase, perform_deletion))
        return d


class BMCRoutableRackControllerRelationship(CleanSave, TimestampedModel):
    """Records the link routable status of a BMC from a RackController.

    When a BMC is first created all rack controllers are check to see which
    have access to the BMC through a route (not directly connected).
    Periodically this information is updated for every rack controller when
    it asks the region controller for the machines it needs to power check.

    The `updated` field is used to track the last time this information was
    updated and if the rack controller should check its routable status
    again. A link will be created between every `BMC` and `RackController` in
    this table to record the last time it was checked and if it was `routable`
    or not.
    """

    bmc = ForeignKey(
        BMC, related_name="routable_rack_relationships", on_delete=CASCADE
    )
    rack_controller = ForeignKey(
        "RackController",
        related_name="routable_bmc_relationships",
        on_delete=CASCADE,
    )
    routable = BooleanField()
