# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC declarations for the region.

These are commands that a region controller ought to respond to.
"""

__all__ = [
    "Authenticate",
    "CommissionNode",
    "CreateNode",
    "GetArchiveMirrors",
    "GetBootConfig",
    "GetBootSources",
    "GetBootSourcesV2",
    "GetControllerType",
    "GetDiscoveryState",
    "GetDNSConfiguration",
    "GetProxies",
    "GetTimeConfiguration",
    "Identify",
    "ListNodePowerParameters",
    "MarkNodeFailed",
    "RegisterEventType",
    "RegisterRackController",
    "ReportBootImages",
    "ReportForeignDHCPServer",
    "ReportMDNSEntries",
    "ReportNeighbours",
    "RequestNodeInfoByMACAddress",
    "SendEvent",
    "SendEventMACAddress",
    "UpdateInterfaces",
    "UpdateLastImageSync",
    "UpdateNodePowerState",
]

from provisioningserver.rpc.arguments import (
    AmpList,
    Bytes,
    ParsedURL,
    StructureAsJSON,
)
from provisioningserver.rpc.common import Authenticate, Identify
from provisioningserver.rpc.exceptions import (
    BootConfigNoResponse,
    CannotRegisterRackController,
    CommissionNodeFailed,
    NodeAlreadyExists,
    NodeStateViolation,
    NoSuchCluster,
    NoSuchEventType,
    NoSuchNode,
)
from twisted.protocols import amp


class RegisterRackController(amp.Command):
    """Register a rack controller with the region controller.

    This is the second step of the Authenticate, Register, Commission
    process.

    :since: 2.0
    """

    arguments = [
        (b"system_id", amp.Unicode(optional=True)),
        (b"hostname", amp.Unicode()),
        (b"interfaces", StructureAsJSON()),
        # The URL for the region as seen by the rack controller.
        (b"url", ParsedURL(optional=True)),
        # The old nodegroup UUID.
        (b"nodegroup_uuid", amp.Unicode(optional=True)),
        (b"beacon_support", amp.Boolean(optional=True)),
        (b"version", amp.Unicode(optional=True)),
    ]
    response = [
        (b"system_id", amp.Unicode()),
        (b"beacon_support", amp.Boolean(optional=True)),
        (b"version", amp.Unicode(optional=True)),
        (b"uuid", amp.Unicode(optional=True)),
    ]
    errors = {CannotRegisterRackController: b"CannotRegisterRackController"}


class ReportBootImages(amp.Command):
    """Report boot images available on the invoking cluster controller.

    :since: 1.5
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode()),
        (
            b"images",
            AmpList(
                [
                    (b"architecture", amp.Unicode()),
                    (b"subarchitecture", amp.Unicode()),
                    (b"release", amp.Unicode()),
                    (b"purpose", amp.Unicode()),
                ]
            ),
        ),
    ]
    response = []
    errors = []


class GetBootConfig(amp.Command):
    """Get the boot configuration for booting machine.

    :since: 2.0
    """

    arguments = [
        # The system_id for the rack controller.
        (b"system_id", amp.Unicode()),
        (b"local_ip", amp.Unicode()),
        (b"remote_ip", amp.Unicode()),
        (b"arch", amp.Unicode(optional=True)),
        (b"subarch", amp.Unicode(optional=True)),
        (b"mac", amp.Unicode(optional=True)),
        (b"hardware_uuid", amp.Unicode(optional=True)),
        (b"bios_boot_method", amp.Unicode(optional=True)),
    ]
    response = [
        (b"arch", amp.Unicode()),
        (b"subarch", amp.Unicode()),
        (b"osystem", amp.Unicode()),
        (b"release", amp.Unicode()),
        # Kernel and initrd are optional as they were introduced
        # in 2.1.1. After 2.2 they can be set to manditory.
        (b"kernel", amp.Unicode(optional=True)),
        (b"initrd", amp.Unicode(optional=True)),
        # boot_dtb is optional as not all archs require it.
        (b"boot_dtb", amp.Unicode(optional=True)),
        (b"purpose", amp.Unicode()),
        (b"hostname", amp.Unicode()),
        (b"domain", amp.Unicode()),
        (b"preseed_url", amp.Unicode()),
        (b"fs_host", amp.Unicode()),
        (b"log_host", amp.Unicode()),
        (b"log_port", amp.Integer(optional=True)),
        (b"extra_opts", amp.Unicode()),
        (b"system_id", amp.Unicode(optional=True)),
        # MAAS 2.3 introduced this parameter to toggle using TGT or HTTP to
        # boot. TGT support was removed in MAAS 2.4 however without this option
        # a MAAS 2.3 rack controller connected to a MAAS 2.4 region controller
        # will try to use TGT as Twisted sets optional parameters to False when
        # not defined.
        (b"http_boot", amp.Boolean(optional=True)),
    ]
    errors = {BootConfigNoResponse: b"BootConfigNoResponse"}


class GetBootSources(amp.Command):
    """Report boot sources and selections for the given cluster.

    :since: 1.6
    :deprecated: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode())
    ]
    response = [
        (
            b"sources",
            AmpList(
                [
                    (b"url", amp.Unicode()),
                    (b"keyring_data", Bytes()),
                    (
                        b"selections",
                        AmpList(
                            [
                                (b"release", amp.Unicode()),
                                (b"arches", amp.ListOf(amp.Unicode())),
                                (b"subarches", amp.ListOf(amp.Unicode())),
                                (b"labels", amp.ListOf(amp.Unicode())),
                            ]
                        ),
                    ),
                ]
            ),
        )
    ]
    errors = []


class GetBootSourcesV2(amp.Command):
    """Report boot sources and selections for the given cluster.

    Includes the new os field for the selections.

    :since: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode())
    ]
    response = [
        (
            b"sources",
            AmpList(
                [
                    (b"url", amp.Unicode()),
                    (b"keyring_data", Bytes()),
                    (
                        b"selections",
                        AmpList(
                            [
                                (b"os", amp.Unicode()),
                                (b"release", amp.Unicode()),
                                (b"arches", amp.ListOf(amp.Unicode())),
                                (b"subarches", amp.ListOf(amp.Unicode())),
                                (b"labels", amp.ListOf(amp.Unicode())),
                            ]
                        ),
                    ),
                ]
            ),
        )
    ]
    errors = []


class GetArchiveMirrors(amp.Command):
    """Return the Main and Port mirrors to use.

    :since: 1.7
    """

    arguments = []
    response = [(b"main", ParsedURL()), (b"ports", ParsedURL())]
    errors = []


class GetProxies(amp.Command):
    """Return the HTTP and HTTPS proxies to use.

    :since: 1.6
    """

    arguments = []
    response = [
        (b"http", ParsedURL(optional=True)),
        (b"https", ParsedURL(optional=True)),
    ]
    errors = []


class MarkNodeFailed(amp.Command):
    """Mark a node as 'broken'.

    :since: 1.7
    """

    arguments = [
        # The node's system_id.
        (b"system_id", amp.Unicode()),
        # The error description.
        (b"error_description", amp.Unicode()),
    ]
    response = []
    errors = {
        NodeStateViolation: b"NodeStateViolation",
        NoSuchNode: b"NoSuchNode",
    }


class ListNodePowerParameters(amp.Command):
    """Return power parameters for the nodes in the specified cluster.

    This will only return power parameters for nodes that have power types for
    which MAAS has a query capability.

    It will return nodes in priority order. Those nodes at the beginning of
    the list should be queried first.

    It may return an empty list. This means that all nodes have been recently
    queried. Take a break before asking again.

    :since: 1.7
    """

    arguments = [
        # The cluster UUID.
        (b"uuid", amp.Unicode())
    ]
    response = [
        (
            b"nodes",
            AmpList(
                [
                    (b"system_id", amp.Unicode()),
                    (b"hostname", amp.Unicode()),
                    (b"power_state", amp.Unicode()),
                    (b"power_type", amp.Unicode()),
                    # We can't define a tighter schema here because this is a highly
                    # variable bag of arguments from a variety of sources.
                    (b"context", StructureAsJSON()),
                ]
            ),
        )
    ]
    errors = {NoSuchCluster: b"NoSuchCluster"}


class UpdateLastImageSync(amp.Command):
    """Update Rack Controller's Last Image Sync.

    :since: 2.0
    """

    arguments = [
        # A rack controller's system_id.
        (b"system_id", amp.Unicode())
    ]
    response = []
    errors = []


class UpdateNodePowerState(amp.Command):
    """Update Node Power State.

    :since: 1.7
    """

    arguments = [
        # The node's system_id.
        (b"system_id", amp.Unicode()),
        # The node's power_state.
        (b"power_state", amp.Unicode()),
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode"}


class RegisterEventType(amp.Command):
    """Register an event type.

    :since: 1.7
    """

    arguments = [
        (b"name", amp.Unicode()),
        (b"description", amp.Unicode()),
        (b"level", amp.Integer()),
    ]
    response = []
    errors = []


class SendEvent(amp.Command):
    """Send an event.

    :since: 1.7
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (b"type_name", amp.Unicode()),
        (b"description", amp.Unicode()),
    ]
    response = []
    errors = {
        # In practice, neither NoSuchNode nor NoSuchEventType will be returned
        # by the region controller as of MAAS 1.9 because the region no longer
        # waits for the database work to complete.
        NoSuchNode: b"NoSuchNode",
        NoSuchEventType: b"NoSuchEventType",
    }


class SendEventMACAddress(amp.Command):
    """Send an event.

    :since: 1.7
    """

    arguments = [
        (b"mac_address", amp.Unicode()),
        (b"type_name", amp.Unicode()),
        (b"description", amp.Unicode()),
    ]
    response = []
    errors = {
        # In practice, neither NoSuchNode nor NoSuchEventType will be returned
        # by the region controller as of MAAS 1.9 because the region no longer
        # waits for the database work to complete.
        NoSuchNode: b"NoSuchNode",
        NoSuchEventType: b"NoSuchEventType",
    }


class SendEventIPAddress(amp.Command):
    """Send an event by IP address.

    :since: 2.5
    """

    arguments = [
        (b"ip_address", amp.Unicode()),
        (b"type_name", amp.Unicode()),
        (b"description", amp.Unicode()),
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode", NoSuchEventType: b"NoSuchEventType"}


class ReportForeignDHCPServer(amp.Command):
    """Report a foreign DHCP server on a rack controller's interface.

    :since: 2.0
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (b"interface_name", amp.Unicode()),
        (b"dhcp_ip", amp.Unicode(optional=True)),
    ]
    response = []
    errors = []


class ReportMDNSEntries(amp.Command):
    """Called by a rack controller to report observed mDNS entries.

    :since: 2.1
    """

    arguments = [(b"system_id", amp.Unicode()), (b"mdns", StructureAsJSON())]
    response = []
    errors = {NoSuchNode: b"NoSuchNode"}


class ReportNeighbours(amp.Command):
    """Called by a rack controller to report observed neighbor devices.

    :since: 2.1
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (b"neighbours", StructureAsJSON()),
    ]
    response = []
    errors = {NoSuchNode: b"NoSuchNode"}


class CreateNode(amp.Command):
    """Create a node on a given cluster.

    :since: 2.0
    """

    arguments = [
        (b"architecture", amp.Unicode()),
        (b"power_type", amp.Unicode()),
        (b"power_parameters", amp.Unicode()),
        (b"mac_addresses", amp.ListOf(amp.Unicode())),
        (b"hostname", amp.Unicode(optional=True)),
        (b"domain", amp.Unicode(optional=True)),
    ]
    response = [(b"system_id", amp.Unicode())]
    errors = {NodeAlreadyExists: b"NodeAlreadyExists"}


class CommissionNode(amp.Command):
    """Commission node.

    :since: 1.7
    """

    arguments = [(b"system_id", amp.Unicode()), (b"user", amp.Unicode())]
    response = []
    errors = {CommissionNodeFailed: b"CommissionNodeFailed"}


class UpdateInterfaces(amp.Command):
    """Called by a rack controller to update its interface definition.

    :since: 2.0
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (b"interfaces", StructureAsJSON()),
        (b"topology_hints", StructureAsJSON(optional=True)),
    ]
    response = []
    errors = []


class GetDiscoveryState(amp.Command):
    """Called by a rack controller to get its interface discovery state.

    :since: 2.1
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [(b"interfaces", StructureAsJSON())]
    errors = {NoSuchNode: b"NoSuchNode"}


class RequestNodeInfoByMACAddress(amp.Command):
    """Request Node information by mac address.

    :since: 1.7
    """

    arguments = [(b"mac_address", amp.Unicode())]
    response = [
        (b"system_id", amp.Unicode()),
        (b"hostname", amp.Unicode()),
        (b"status", amp.Integer()),
        (b"boot_type", amp.Unicode()),
        (b"osystem", amp.Unicode()),
        (b"distro_series", amp.Unicode()),
        (b"architecture", amp.Unicode()),
        (b"purpose", amp.Unicode()),
    ]
    errors = {NoSuchNode: b"NoSuchNode"}


class UpdateLease(amp.Command):
    """Report DHCP lease update from a cluster controller. Different from
    `UpdateLeases` as this call only updates one lease at a time.

    :since: 2.0
    """

    arguments = [
        (b"cluster_uuid", amp.Unicode()),
        (b"action", amp.Unicode()),
        (b"mac", amp.Unicode()),
        (b"ip_family", amp.Unicode()),
        (b"ip", amp.Unicode()),
        (b"timestamp", amp.Integer()),
        (b"lease_time", amp.Integer(optional=True)),
        (b"hostname", amp.Unicode(optional=True)),
    ]
    response = []
    errors = {NoSuchCluster: b"NoSuchCluster"}


class UpdateServices(amp.Command):
    """Report service statuses that are monitored on the rackd.

    :since: 2.0
    """

    arguments = [
        (b"system_id", amp.Unicode()),
        (
            b"services",
            AmpList(
                [
                    (b"name", amp.Unicode()),
                    (b"status", amp.Unicode()),
                    (b"status_info", amp.Unicode()),
                ]
            ),
        ),
    ]
    response = []
    errors = {NoSuchCluster: b"NoSuchCluster"}


class RequestRackRefresh(amp.Command):
    """Request a refresh of the rack from the region.

    :since: 2.0
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = []
    errors = []


class GetControllerType(amp.Command):
    """Get the node type for a given system identifier.

    :since: 2.1
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [(b"is_region", amp.Boolean()), (b"is_rack", amp.Boolean())]
    errors = {NoSuchNode: b"NoSuchNode"}


class GetTimeConfiguration(amp.Command):
    """Get settings to use for configuring NTP for a given system identifier.

    :since: 2.1
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [
        (b"servers", amp.ListOf(amp.Unicode())),
        (b"peers", amp.ListOf(amp.Unicode())),
    ]
    errors = {NoSuchNode: b"NoSuchNode"}


class GetDNSConfiguration(amp.Command):
    """Get settings to use for configuring DNS for a given system identifier.

    :since: 2.5
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [(b"trusted_networks", amp.ListOf(amp.Unicode()))]
    errors = {NoSuchNode: b"NoSuchNode"}


class GetProxyConfiguration(amp.Command):
    """Get settings to use for configuring proxy for a given system identifier.

    :since: 2.5
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [
        (b"enabled", amp.Boolean()),
        (b"port", amp.Integer()),
        (b"allowed_cidrs", amp.ListOf(amp.Unicode())),
        (b"prefer_v4_proxy", amp.Boolean()),
    ]
    errors = {NoSuchNode: b"NoSuchNode"}


class GetSyslogConfiguration(amp.Command):
    """Get settings to use for configuring syslog for a given system identifier.

    :since: 2.5
    """

    arguments = [(b"system_id", amp.Unicode())]
    response = [(b"port", amp.Integer())]
    errors = {NoSuchNode: b"NoSuchNode"}
