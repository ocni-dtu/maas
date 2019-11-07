# Copyright 2016-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC helpers relating to rack controllers."""

__all__ = [
    "handle_upgrade",
    "register",
    "update_interfaces",
    "update_last_image_sync",
]

from typing import Optional

from django.db.models import Q
from maasserver import locks, worker_user
from maasserver.enum import NODE_TYPE
from maasserver.models import (
    ControllerInfo,
    Domain,
    Node,
    NodeGroupToRackController,
    RackController,
    RegionController,
    StaticIPAddress,
)
from maasserver.models.timestampedmodel import now
from maasserver.utils import synchronised
from maasserver.utils.orm import transactional, with_connection
from metadataserver.models import ScriptSet
from provisioningserver.logger import get_maas_logger
from provisioningserver.rpc.exceptions import NoSuchNode
from provisioningserver.utils import typed
from provisioningserver.utils.twisted import synchronous


maaslog = get_maas_logger("rpc.rackcontrollers")


@synchronous
@transactional
def handle_upgrade(rack_controller, nodegroup_uuid):
    """Handle upgrading from MAAS 1.9. Set the VLAN the rack controller
    should manage."""
    if (
        nodegroup_uuid is not None
        and len(nodegroup_uuid) > 0
        and not nodegroup_uuid.isspace()
    ):
        ng_to_racks = NodeGroupToRackController.objects.filter(
            uuid=nodegroup_uuid
        )
        vlans = [ng_to_rack.subnet.vlan for ng_to_rack in ng_to_racks]
        # The VLAN object can only be related to a RackController
        for nic in rack_controller.interface_set.all():
            if nic.vlan in vlans:
                nic.vlan.primary_rack = rack_controller
                nic.vlan.dhcp_on = True
                nic.vlan.save()
                maaslog.info(
                    "DHCP setting from NodeGroup(%s) have been migrated "
                    "to %s." % (nodegroup_uuid, nic.vlan)
                )
        for ng_to_rack in ng_to_racks:
            ng_to_rack.delete()


@synchronous
@with_connection
@synchronised(locks.startup)
@transactional
def register(
    system_id=None,
    hostname="",
    interfaces=None,
    url=None,
    is_loopback=None,
    create_fabrics=True,
    version=None,
):
    """Register a new rack controller if not already registered.

    Attempt to see if the rack controller was already registered as a node.
    This can be looked up either by system_id, hostname, or mac address. If
    found convert the existing node into a rack controller. If not found
    create a new rack controller. After the rack controller has been
    registered and successfully connected we will refresh all commissioning
    data.

    The parameter ``is_loopback`` is only referenced if ``url`` is not None.

    :return: A ``rack-controller``.
    """
    if interfaces is None:
        interfaces = {}

    # If hostname is actually a FQDN, split the domain off and
    # create it as non-authoritative domain if it does not exist already.
    domain = Domain.objects.get_default_domain()
    if hostname.find(".") > 0:
        hostname, domainname = hostname.split(".", 1)
        (domain, _) = Domain.objects.get_or_create(
            name=domainname, defaults={"authoritative": False}
        )

    this_region = RegionController.objects.get_running_controller()
    node = find(system_id, hostname, interfaces)
    version_log = "2.2 or below" if version is None else version
    if node is None:
        node = RackController.objects.create(hostname=hostname, domain=domain)
        maaslog.info(
            "New rack controller '%s' running version %s was created by "
            "region '%s' upon first connection.",
            node.hostname,
            version_log,
            this_region.hostname,
        )
    elif node.is_rack_controller:
        # Only the master process logs to the maaslog.
        maaslog.info(
            "Existing rack controller '%s' running version %s has "
            "connected to region '%s'.",
            node.hostname,
            version_log,
            this_region.hostname,
        )
    elif node.is_region_controller:
        maaslog.info(
            "Region controller '%s' running version %s converted into a "
            "region and rack controller.",
            node.hostname,
            version_log,
        )
        node.node_type = NODE_TYPE.REGION_AND_RACK_CONTROLLER
        node.pool = None
        node.save()
    else:
        maaslog.info(
            "Region controller '%s' converted '%s' running version %s into a "
            "rack controller.",
            this_region.hostname,
            node.hostname,
            version_log,
        )
        node.node_type = NODE_TYPE.RACK_CONTROLLER
        node.pool = None
        node.save()

    if node.current_commissioning_script_set is None:
        # Create a ScriptSet so the rack can store its commissioning data
        # which is sent on connect.
        script_set = ScriptSet.objects.create_commissioning_script_set(node)
        node.current_commissioning_script_set = script_set
        node.save()

    rackcontroller = node.as_rack_controller()

    # Update `rackcontroller.url` from the given URL, if it has changed.
    if url is not None:
        if is_loopback:
            rackcontroller.url = ""
        elif not is_loopback:
            rackcontroller.url = url.geturl()
    if rackcontroller.owner is None:
        rackcontroller.owner = worker_user.get_worker_user()
    rackcontroller.save()
    # Update interfaces, if requested.
    rackcontroller.update_interfaces(interfaces, create_fabrics=create_fabrics)
    # Update the version.
    if version is not None:
        ControllerInfo.objects.set_version(rackcontroller, version)
    return rackcontroller


@typed
def find(system_id: Optional[str], hostname: str, interfaces: dict):
    """Find an existing node by `system_id`, `hostname`, and `interfaces`.

    :type system_id: str or None
    :type hostname: str
    :type interfaces: dict
    :return: An instance of :class:`Node` or `None`
    """
    mac_addresses = {
        interface["mac_address"]
        for interface in interfaces.values()
        if "mac_address" in interface
    }
    query = (
        Q(system_id=system_id)
        | Q(hostname=hostname)
        | Q(interface__mac_address__in=mac_addresses)
    )
    return Node.objects.filter(query).first()


@transactional
def update_foreign_dhcp(system_id, interface_name, dhcp_ip=None):
    """Update the external_dhcp field of the VLAN for the interface.

    :param system_id: Rack controller system_id.
    :param interface_name: The name of the interface.
    :param dhcp_ip: The IP address of the responding DHCP server.
    """
    rack_controller = RackController.objects.get(system_id=system_id)
    interface = (
        rack_controller.interface_set.filter(name=interface_name)
        .select_related("vlan")
        .first()
    )
    if interface is not None:
        if dhcp_ip is not None:
            sip = StaticIPAddress.objects.filter(ip=dhcp_ip).first()
            if sip is not None:
                # Check that its not an IP address of a rack controller
                # providing that DHCP service.
                rack_interfaces_serving_dhcp = sip.interface_set.filter(
                    node__node_type__in=[
                        NODE_TYPE.RACK_CONTROLLER,
                        NODE_TYPE.REGION_AND_RACK_CONTROLLER,
                    ],
                    vlan__dhcp_on=True,
                )
                if rack_interfaces_serving_dhcp.exists():
                    # Not external. It's a MAAS DHCP server.
                    dhcp_ip = None
        if interface.vlan is None:
            maaslog.warning(
                "%s: Detected an external DHCP server on an interface with no "
                "VLAN defined: '%s': %s"
                % (
                    rack_controller.hostname,
                    interface.get_log_string(),
                    dhcp_ip,
                )
            )
        else:
            if interface.vlan.external_dhcp != dhcp_ip:
                interface.vlan.external_dhcp = dhcp_ip
                interface.vlan.save()


@synchronous
@transactional
def update_interfaces(system_id, interfaces, topology_hints=None):
    """Update the interface definition on the rack controller."""
    rack_controller = RackController.objects.get(system_id=system_id)
    rack_controller.update_interfaces(interfaces, topology_hints)


@synchronous
@transactional
def get_discovery_state(system_id):
    """Update the interface definition on the rack controller."""
    rack_controller = RackController.objects.get(system_id=system_id)
    return rack_controller.get_discovery_state()


@synchronous
@transactional
def report_mdns_entries(system_id, mdns):
    """Report observed neighbours seen on the rack controller."""
    try:
        rack_controller = RackController.objects.get(system_id=system_id)
    except RackController.DoesNotExist:
        raise NoSuchNode.from_system_id(system_id)
    else:
        rack_controller.report_mdns_entries(mdns)


@synchronous
@transactional
def report_neighbours(system_id, neighbours):
    """Report observed neighbours seen on the rack controller."""
    try:
        rack_controller = RackController.objects.get(system_id=system_id)
    except RackController.DoesNotExist:
        raise NoSuchNode.from_system_id(system_id)
    else:
        rack_controller.report_neighbours(neighbours)


@synchronous
@transactional
def update_last_image_sync(system_id):
    """Update rack controller's last_image_sync.

    for :py:class:`~provisioningserver.rpc.region.UpdateLastImageSync.
    """
    RackController.objects.filter(system_id=system_id).update(
        last_image_sync=now()
    )
