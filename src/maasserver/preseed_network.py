# Copyright 2015-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Preseed generation for curtin/netplan network."""

__all__ = []

from collections import defaultdict, OrderedDict
from operator import attrgetter

from maasserver.dns.zonegenerator import get_dns_search_paths
from maasserver.enum import (
    BRIDGE_TYPE,
    INTERFACE_TYPE,
    IPADDRESS_FAMILY,
    IPADDRESS_TYPE,
    NODE_STATUS,
)
from maasserver.models import Interface
from maasserver.models.staticroute import StaticRoute
from netaddr import IPNetwork
from provisioningserver.utils.netplan import (
    get_netplan_bond_parameters,
    get_netplan_bridge_parameters,
)
from provisioningserver.utils.network import get_source_address
import yaml


def _is_link_up(addresses):
    """Return True if the interface should be in LINK_UP mode.

    :param addresses: A list of StaticIPAddress model objects.
    """
    if len(addresses) == 0:
        return True
    elif len(addresses) == 1:
        address = addresses[0]
        if address.alloc_type == IPADDRESS_TYPE.STICKY and not address.ip:
            return True
    return False


def _get_param_value(value):
    """Return correct value based on type of `value`."""
    if isinstance(value, (bytes, str)):
        return value
    elif isinstance(value, bool):
        return 1 if value else 0
    else:
        return value


def _generate_route_operation(route, version=1):
    """Generate route operation place in `network_config`."""
    if version == 1:
        route_operation = {
            "destination": route.destination.cidr,
            "gateway": route.gateway_ip,
            "metric": route.metric,
        }
        return route_operation
    elif version == 2:
        route_operation = {
            "to": route.destination.cidr,
            "via": route.gateway_ip,
            "metric": route.metric,
        }
        return route_operation


class InterfaceConfiguration:
    def __init__(self, iface, node_config, version=1, source_routing=False):
        """

        :param iface: The interface whose configuration to generate.
        :param routes: Static routes present on the system.
        """
        self.iface = iface
        self.type = iface.type
        self.id = iface.id
        self.node_config = node_config
        self.routes = node_config.routes
        self.gateways = node_config.gateways
        self.secondary_gateway_routes = []
        self.secondary_gateway_policies = []
        # Note: the matching routes are populated in _generate_addresses().
        # (Only routes with an on-link gateway can be configured.)
        self.matching_routes = set()
        self.addr_family_present = defaultdict(bool)
        self.version = version
        self.source_routing = source_routing
        self.config = None
        self.name = self.iface.get_name()

        if self.type == INTERFACE_TYPE.PHYSICAL:
            self.config = self._generate_physical_operation(version=version)
        elif self.type == INTERFACE_TYPE.VLAN:
            self.config = self._generate_vlan_operation(version=version)
        elif self.type == INTERFACE_TYPE.BOND:
            self.config = self._generate_bond_operation(version=version)
        elif self.type == INTERFACE_TYPE.BRIDGE:
            self.config = self._generate_bridge_operation(version=version)
        else:
            raise ValueError("Unknown interface type: %s" % self.type)

        if version == 2:
            routes = self._generate_route_operations(
                self.matching_routes, version=version
            )
            routes.extend(self.secondary_gateway_routes)
            if len(routes) > 0:
                self.config["routes"] = routes
            if len(self.secondary_gateway_policies) > 0:
                self.config["routing-policy"] = self.secondary_gateway_policies

    def _generate_route_operations(self, matching_routes, version=1):
        """Generate all route operations."""
        routes = []
        for route in sorted(matching_routes, key=attrgetter("id")):
            routes.append(_generate_route_operation(route, version=version))
        return routes

    def _generate_physical_operation(self, version=1):
        """Generate physical interface operation for `interface` and place in
        `network_config`."""
        addrs = self._generate_addresses(version=version)
        physical_operation = self._get_initial_params()
        if version == 1:
            physical_operation.update(
                {
                    "id": self.name,
                    "type": "physical",
                    "name": self.name,
                    "mac_address": str(self.iface.mac_address),
                }
            )
            if addrs:
                physical_operation["subnets"] = addrs
        elif version == 2:
            physical_operation.update(
                {
                    "match": {"macaddress": str(self.iface.mac_address)},
                    # Unclear what we want, so just let it be the default.
                    # "wakeonlan": True,
                    "set-name": self.name,
                }
            )
            physical_operation.update(addrs)
        return physical_operation

    def _get_dhcp_type(self):
        """Return the DHCP type for the interface."""
        dhcp_types = set()
        if (
            self.iface == self.iface.node.boot_interface
            and NODE_STATUS.COMMISSIONING
            in {self.iface.node.status, self.iface.node.previous_status}
            and self.iface.ip_addresses.exclude(
                alloc_type=IPADDRESS_TYPE.DISCOVERED, ip=None
            ).exists()
        ):
            # AUTOIP assignment happens as a post_commit() hook after a node
            # starts testing or deploying so MAAS can verify the IP address is
            # free. If it is requested during commissioning or in testing run
            # directly after commissioning no IP will be assigned when
            # networking configuration is reset. In this state generate a
            # configuration file with dhcp being run on the boot interface.
            # This is the same configuration run at boot so testing will be
            # done with the booted configuration.
            qs = self.iface.ip_addresses.all()
        else:
            qs = self.iface.ip_addresses.filter(
                alloc_type=IPADDRESS_TYPE.DHCP
            ).select_related("subnet")

        for dhcp_ip in qs:
            if dhcp_ip.subnet is None:
                # No subnet is linked so no IP family can be determined. So
                # we allow both families to be DHCP'd.
                dhcp_types.add(4)
                dhcp_types.add(6)
                self.addr_family_present[4] = True
                self.addr_family_present[6] = True
            else:
                dhcp_types.add(dhcp_ip.subnet.get_ipnetwork().version)

        if dhcp_types == {4, 6}:
            self.addr_family_present[4] = True
            self.addr_family_present[6] = True
            return "dhcp"
        elif dhcp_types == {4}:
            self.addr_family_present[4] = True
            return "dhcp4"
        elif dhcp_types == {6}:
            self.addr_family_present[6] = True
            return "dhcp6"
        else:
            return None

    def _get_default_gateway(self, subnet, include_secondary=False):
        """Return a gateway that should be added for the specified subnet.

        If no relevant gateway is found, returns None.
        """
        if subnet.gateway_ip:
            for gateway in self.gateways:
                if gateway is None:
                    continue
                if isinstance(gateway, list):
                    # If this is a list, it's the list of secondary gateways.
                    if include_secondary:
                        for gw in gateway:
                            iface_id, subnet_id, gateway_ip = gw
                            if (
                                iface_id == self.id
                                and subnet_id
                                and subnet.id
                                and gateway_ip
                                and subnet.gateway_ip
                            ):
                                return subnet.gateway_ip
                    else:
                        # Caller didn't ask for secondary gateways.
                        continue
                iface_id, subnet_id, gateway_ip = gateway
                if (
                    iface_id == self.id
                    and subnet_id
                    and subnet.id
                    and gateway_ip
                    and subnet.gateway_ip
                ):
                    return subnet.gateway_ip
        return None

    def _set_default_gateway(self, subnet, config, version=1):
        """Set the default gateway on the `subnet_operation` if it should
        be set."""
        network = subnet.get_ipnetwork()
        family = network.version
        node_config = self.node_config
        if not self.source_routing:
            if family == 4 and node_config.gateway_ipv4_set:
                return
            elif family == 6 and node_config.gateway_ipv6_set:
                return
        gateway = self._get_default_gateway(subnet)
        if gateway is not None:
            if version == 1:
                config["gateway"] = str(gateway)
            if family == IPADDRESS_FAMILY.IPv4:
                node_config.gateway_ipv4_set = True
                if version == 2:
                    config["gateway4"] = str(gateway)
            elif family == IPADDRESS_FAMILY.IPv6:
                node_config.gateway_ipv6_set = True
                if version == 2:
                    config["gateway6"] = str(gateway)
        elif version == 2 and self.source_routing:
            # Check if we should select a secondary gateway for use with
            # source routing.
            secondary_gateway = self._get_default_gateway(
                subnet, include_secondary=True
            )
            if secondary_gateway is not None:
                # Allocate another routing table for this gateway.
                table_id = self.node_config.get_next_routing_table_id()
                self.secondary_gateway_routes.append(
                    {
                        "to": "0.0.0.0/0",
                        "via": str(secondary_gateway),
                        "table": table_id,
                    }
                )
                self.secondary_gateway_policies.append(
                    {"from": str(network), "table": table_id, "priority": 100}
                )
                self.secondary_gateway_policies.append(
                    {"from": str(network), "to": str(network), "table": 254}
                )

    def _get_matching_routes(self, source):
        """Return all route objects matching `source`."""
        return {route for route in self.routes if route.source == source}

    def _generate_addresses(self, version=1):
        """Generate the various addresses needed for this interface."""
        v1_config = []
        v2_cidrs = []
        v2_config = {}
        v2_nameservers = {}
        addresses = list(
            self.iface.ip_addresses.exclude(
                alloc_type__in=[IPADDRESS_TYPE.DISCOVERED, IPADDRESS_TYPE.DHCP]
            ).order_by("id")
        )
        dhcp_type = self._get_dhcp_type()
        if _is_link_up(addresses) and not dhcp_type:
            if version == 1:
                v1_config.append({"type": "manual"})
        else:
            for address in addresses:
                subnet = address.subnet
                if subnet is not None:
                    subnet_len = subnet.cidr.split("/")[1]
                    cidr = "%s/%s" % (str(address.ip), subnet_len)
                    v1_subnet_operation = {"type": "static", "address": cidr}
                    if address.ip is not None:
                        # If the address is None, that means we're generating a
                        # preseed for a Node that is not (or is no longer) in
                        # the READY state; so it might have auto-assigned IP
                        # addresses which have not yet been determined. It
                        # would be nice if there was a way to express this, if
                        # only for debugging purposes. For now, just ignore
                        # such addresses.
                        v1_subnet_operation["address"] = cidr
                        v2_cidrs.append(cidr)
                        if "addresses" not in v2_config:
                            v2_config["addresses"] = v2_cidrs
                    v1_config.append(v1_subnet_operation)
                    self.addr_family_present[
                        IPNetwork(subnet.cidr).version
                    ] = True
                    # The default gateway is set on the subnet operation for
                    # the v1 YAML, but it's per-interface for the v2 YAML.
                    self._set_default_gateway(
                        subnet,
                        v1_subnet_operation if version == 1 else v2_config,
                        version,
                    )
                    if (
                        subnet.dns_servers is not None
                        and len(subnet.dns_servers) > 0
                    ):
                        v1_subnet_operation[
                            "dns_nameservers"
                        ] = subnet.dns_servers
                        v1_subnet_operation[
                            "dns_search"
                        ] = self.node_config.default_search_list
                        if "nameservers" not in v2_config:
                            v2_config["nameservers"] = v2_nameservers
                            v2_config["nameservers"][
                                "search"
                            ] = self.node_config.default_search_list
                            if "addresses" not in v2_nameservers:
                                v2_nameservers["addresses"] = []
                        v2_nameservers["addresses"].extend(
                            [server for server in subnet.dns_servers]
                        )
                    matching_subnet_routes = self._get_matching_routes(subnet)
                    if len(matching_subnet_routes) > 0 and version == 1:
                        # For the v1 YAML, the list of routes is rendered
                        # within the context of each subnet.
                        routes = self._generate_route_operations(
                            matching_subnet_routes, version=version
                        )
                        v1_subnet_operation["routes"] = routes
                    # Keep track of routes which apply to the context of this
                    # interface for rendering the v2 YAML.
                    self.matching_routes.update(matching_subnet_routes)
            if dhcp_type:
                v1_config.append({"type": dhcp_type})
                if dhcp_type == "dhcp":
                    v2_config.update({"dhcp4": True, "dhcp6": True})
                elif dhcp_type == "dhcp4":
                    v2_config.update({"dhcp4": True})
                elif dhcp_type == "dhcp6":
                    v2_config.update({"dhcp6": True})
        if version == 1:
            return v1_config
        elif version == 2:
            return v2_config

    def _generate_vlan_operation(self, version=1):
        """Generate vlan operation for `iface` and place in
        `network_config`."""
        vlan = self.iface.vlan
        name = self.name
        addrs = self._generate_addresses(version=version)
        vlan_operation = self._get_initial_params()
        if version == 1:
            vlan_operation.update(
                {
                    "id": name,
                    "type": "vlan",
                    "name": name,
                    "vlan_link": self.iface.parents.first().get_name(),
                    "vlan_id": vlan.vid,
                }
            )
            if addrs:
                vlan_operation["subnets"] = addrs
        elif version == 2:
            vlan_operation.update(
                {"id": vlan.vid, "link": self.iface.parents.first().get_name()}
            )
            vlan_operation.update(addrs)
        return vlan_operation

    def _generate_bond_operation(self, version=1):
        """Generate bond operation for `iface` and place in
        `network_config`."""
        addrs = self._generate_addresses(version=version)
        bond_operation = self._get_initial_params()
        if version == 1:
            bond_operation.update(
                {
                    "id": self.name,
                    "type": "bond",
                    "name": self.name,
                    "mac_address": str(self.iface.mac_address),
                    "bond_interfaces": [
                        parent.get_name()
                        for parent in self.iface.parents.order_by("name")
                    ],
                    "params": self._get_bond_params(),
                }
            )
            if addrs:
                bond_operation["subnets"] = addrs
        else:
            bond_operation.update(
                {
                    "macaddress": str(self.iface.mac_address),
                    "interfaces": [
                        parent.get_name()
                        for parent in self.iface.parents.order_by("name")
                    ],
                }
            )
            bond_params = get_netplan_bond_parameters(self._get_bond_params())
            if len(bond_params) > 0:
                bond_operation["parameters"] = bond_params
            bond_operation.update(addrs)
        return bond_operation

    def _generate_bridge_operation(self, version=1):
        """Generate bridge operation for this interface."""
        addrs = self._generate_addresses(version=version)
        bridge_operation = self._get_initial_params()
        if version == 1:
            bridge_operation.update(
                {
                    "id": self.name,
                    "type": "bridge",
                    "name": self.name,
                    "mac_address": str(self.iface.mac_address),
                    "bridge_interfaces": [
                        parent.get_name()
                        for parent in self.iface.parents.order_by("name")
                    ],
                    "params": self._get_bridge_params(version=version),
                }
            )
            if addrs:
                bridge_operation["subnets"] = addrs
        elif version == 2:
            bridge_operation.update(
                {
                    "macaddress": str(self.iface.mac_address),
                    "interfaces": [
                        parent.get_name()
                        for parent in self.iface.parents.order_by("name")
                    ],
                }
            )
            if self.iface.params:
                bridge_type = self.iface.params.get(
                    "bridge_type", BRIDGE_TYPE.STANDARD
                )
                if bridge_type == BRIDGE_TYPE.OVS:
                    bridge_operation.update({"renderer": "openvswitch"})
            bridge_params = get_netplan_bridge_parameters(
                self._get_bridge_params(version=version)
            )
            if len(bridge_params) > 0:
                bridge_operation["parameters"] = bridge_params
            bridge_operation.update(addrs)
        return bridge_operation

    def _get_initial_params(self):
        """Return the starting parameters for the interface.

        This is done by extracting parameters from the `params` property on
        the `interface`. This is done before all the other parameters are added
        so any colliding parameters will be overridden.
        """
        params = {}
        if self.iface.params:
            for key, value in self.iface.params.items():
                # Don't include bond or bridge parameters.
                if (
                    not key.startswith("bond_")
                    and not key.startswith("bridge_")
                    and key != "mtu"
                ):
                    params[key] = _get_param_value(value)
        params["mtu"] = self.iface.get_effective_mtu()
        return params

    def _get_bond_params(self):
        params = {}
        if self.iface.params:
            for key, value in self.iface.params.items():
                # Only include bond parameters.
                if key.startswith("bond_"):
                    # Bond parameters are seperated with '-' instead of '_'
                    # which MAAS uses to keep consistent with bridges.
                    params[key.replace("_", "-")] = _get_param_value(value)
        bond_mode = params.get("bond-mode")
        if bond_mode is not None:
            # Bug #1730626: lacp-rate should only be set on 802.3ad bonds.
            if bond_mode != "802.3ad":
                params.pop("bond-lacp-rate", None)
            # Bug #1730991: these parameters only apply to active-backup mode.
            if bond_mode != "active-backup":
                params.pop("bond-num-grat-arp", None)
                params.pop("bond-num-unsol-na", None)
        return params

    def _get_bridge_params(self, version=1):
        params = {}
        if self.iface.params:
            for key, value in self.iface.params.items():
                # Only include bridge parameters.
                if key.startswith("bridge_") and key != "bridge_type":
                    if version == 1:
                        # The v1 YAML needs an extra translation layer (for
                        # example, it changes bool to int).
                        params[key] = _get_param_value(value)
                    else:
                        params[key] = value
        return params


class NodeNetworkConfiguration:
    """Generator for the YAML network configuration for curtin."""

    def __init__(self, node, version=1, source_routing=False):
        """Create the YAML network configuration for the specified node, and
        store it in the `config` ivar.
        """
        self.node = node
        self.matching_routes = set()
        self.v1_config = []
        self.v2_config = [("version", 2)]
        self.v2_ethernets = {}
        self.v2_vlans = {}
        self.v2_bonds = {}
        self.v2_bridges = {}
        # Reserved routing tables in Linux are 0, 253, 254, and 255.
        self.next_routing_table_id = 1
        self.gateway_ipv4_set = False
        self.gateway_ipv6_set = False
        self.source_routing = source_routing

        # The default value is False: expected keys are 4 and 6.
        self.addr_family_present = defaultdict(bool)

        # Ensure the machine's primary domain always comes first in the list.
        self.default_search_list = [self.node.domain.name] + [
            name
            for name in sorted(get_dns_search_paths())
            if name != self.node.domain.name
        ]

        self.gateways = self.node.get_default_gateways()
        if self.gateways.ipv4 is not None:
            dest_ip = self.gateways.ipv4.gateway_ip
        elif self.gateways.ipv6 is not None:
            dest_ip = self.gateways.ipv6.gateway_ip
        else:
            dest_ip = None
        if dest_ip is not None:
            default_source_ip = get_source_address(dest_ip)
        else:
            default_source_ip = None

        self.routes = StaticRoute.objects.all()

        interfaces = Interface.objects.all_interfaces_parents_first(self.node)
        for iface in interfaces:
            if not iface.is_enabled():
                continue
            generator = InterfaceConfiguration(
                iface,
                self,
                version=version,
                source_routing=self.source_routing,
            )
            self.matching_routes.update(generator.matching_routes)
            self.addr_family_present.update(generator.addr_family_present)
            if version == 1:
                self.v1_config.append(generator.config)
            elif version == 2:
                v2_config = {generator.name: generator.config}
                if generator.type == INTERFACE_TYPE.PHYSICAL:
                    self.v2_ethernets.update(v2_config)
                elif generator.type == INTERFACE_TYPE.VLAN:
                    self.v2_vlans.update(v2_config)
                elif generator.type == INTERFACE_TYPE.BOND:
                    self.v2_bonds.update(v2_config)
                elif generator.type == INTERFACE_TYPE.BRIDGE:
                    self.v2_bridges.update(v2_config)

        # If we have no IPv6 addresses present, make sure we claim IPv4, so
        # that we at least get some address.
        if not self.addr_family_present[6]:
            self.addr_family_present[4] = True
        self.default_dns_servers = self.node.get_default_dns_servers(
            ipv4=self.addr_family_present[4],
            ipv6=self.addr_family_present[6],
            default_region_ip=default_source_ip,
        )
        self.v1_config.append(
            {
                "type": "nameserver",
                "address": self.default_dns_servers,
                "search": self.default_search_list,
            }
        )
        if version == 1:
            network_config = {
                "network": {"version": 1, "config": self.v1_config}
            }
        else:
            if len(self.v2_ethernets) > 0:
                self.v2_config.append(("ethernets", self.v2_ethernets))
            if len(self.v2_vlans) > 0:
                self.v2_config.append(("vlans", self.v2_vlans))
            if len(self.v2_bonds) > 0:
                self.v2_config.append(("bonds", self.v2_bonds))
            if len(self.v2_bridges) > 0:
                self.v2_config.append(("bridges", self.v2_bridges))
            self.set_v2_default_dns()
            network_config = {"network": OrderedDict(self.v2_config)}
        self.config = network_config

    def get_next_routing_table_id(self):
        next_table_id = self.next_routing_table_id
        self.next_routing_table_id += 1
        if next_table_id >= 253:
            raise IndexError("Maximum number of routing tables exceeded.")
        return next_table_id

    def set_v2_default_dns(self):
        """Define default nameservers on each interface.

        Define nameservers consistent with how cloud-init does it.
        (See also bug #1664806.)
        """
        # See also:
        # https://git.launchpad.net/cloud-init/commit/?id=d29eeccd
        if (
            len(self.default_dns_servers) > 0
            or len(self.default_search_list) > 0
        ):
            v2_default_nameservers = {}
            if len(self.default_search_list) > 0:
                v2_default_nameservers.update(
                    {"search": self.default_search_list}
                )
            if len(self.default_dns_servers) > 0:
                v2_default_nameservers.update(
                    {"addresses": self.default_dns_servers}
                )
            sections = [
                self.v2_ethernets,
                self.v2_vlans,
                self.v2_bonds,
                self.v2_bridges,
            ]
            for section in sections:
                for ifname, config in section.items():
                    if "nameservers" in config:
                        # Skip interfaces that already have nameservers.
                        continue
                    if "addresses" not in config:
                        # Skip interfaces with no manual addresses.
                        continue
                    config.update({"nameservers": v2_default_nameservers})


def compose_curtin_network_config(node, version=1, source_routing=False):
    """Compose the network configuration for curtin."""
    generator = NodeNetworkConfiguration(
        node, version=version, source_routing=source_routing
    )
    curtin_config = {
        "network_commands": {"builtin": ["curtin", "net-meta", "custom"]}
    }
    curtin_config.update(generator.config)
    # Render the resulting YAML.
    curtin_config_yaml = yaml.safe_dump(
        curtin_config, default_flow_style=False
    )
    return [curtin_config_yaml]
