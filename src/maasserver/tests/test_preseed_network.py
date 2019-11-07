# Copyright 2015-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test `maasserver.preseed_network`."""

__all__ = []

from collections import OrderedDict
import random
from textwrap import dedent

from maasserver import (
    preseed_network as preseed_network_module,
    server_address as server_address_module,
)
from maasserver.dns.zonegenerator import get_dns_search_paths
from maasserver.enum import (
    BRIDGE_TYPE,
    INTERFACE_TYPE,
    IPADDRESS_FAMILY,
    IPADDRESS_TYPE,
    NODE_STATUS,
)
from maasserver.models import Domain
from maasserver.preseed_network import (
    compose_curtin_network_config,
    NodeNetworkConfiguration,
)
import maasserver.server_address
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from netaddr import IPAddress, IPNetwork
from testtools import ExpectedException
from testtools.matchers import (
    ContainsDict,
    Equals,
    IsInstance,
    MatchesDict,
    MatchesListwise,
)
import yaml


class AssertNetworkConfigMixin:

    # MAC addresses are quoted because unquoted all-numeric MAC addresses —
    # e.g. 12:34:56:78:90:12 — are interpreted as *integers* by PyYAML. This
    # edge-case took some time to figure out, and begs the question: why is
    # this YAML being constructed by hacking text strings anyway?

    IFACE_CONFIG = dedent(
        """\
        - id: %(name)s
          name: %(name)s
          type: physical
          mac_address: '%(mac)s'
        """
    )

    BOND_CONFIG = dedent(
        """\
        - id: %(name)s
          name: %(name)s
          type: bond
          mac_address: '%(mac)s'
          bond_interfaces:
        """
    )

    BRIDGE_CONFIG = dedent(
        """\
        - id: %(name)s
          name: %(name)s
          type: bridge
          mac_address: '%(mac)s'
          bridge_interfaces:
        """
    )

    VLAN_CONFIG = dedent(
        """\
        - id: %(name)s
          name: %(name)s
          type: vlan
          vlan_link: %(parent)s
          vlan_id: %(vlan_id)s
        """
    )

    ROUTE_CONFIG = dedent(
        """\
        - id: %(id)s
          type: route
          destination: %(destination)s
          gateway: %(gateway)s
          metric: %(metric)s
        """
    )

    def stripMACs(self, config):
        for entry in config:
            if "mac_address" in entry:
                entry["mac_address"] = "*match*"
        return config

    def stripIPs(self, config):
        for entry in config:
            for subnet in entry.get("subnets", []):
                if "address" in subnet:
                    subnet["address"] = "*match*"
        return config

    def assertNetworkConfig(
        self, expected, output, strip_macs=False, strip_ips=False
    ):
        output = output[0]
        output = yaml.safe_load(output)
        self.assertThat(
            output,
            ContainsDict(
                {
                    "network_commands": MatchesDict(
                        {"builtin": Equals(["curtin", "net-meta", "custom"])}
                    ),
                    "network": MatchesDict(
                        {"version": Equals(1), "config": IsInstance(list)}
                    ),
                }
            ),
        )
        expected_network = yaml.safe_load(expected)
        output_network = output["network"]["config"]
        if strip_macs:
            expected_network = self.stripMACs(expected_network)
            output_network = self.stripMACs(output_network)
        if strip_ips:
            expected_network = self.stripIPs(expected_network)
            output_network = self.stripIPs(output_network)
        expected_equals = list(map(Equals, expected_network))
        self.assertThat(output_network, MatchesListwise(expected_equals))

    def collect_interface_config(self, node, filter="physical"):
        interfaces = node.interface_set.filter(enabled=True).order_by("name")
        if filter:
            interfaces = interfaces.filter(type=filter)

        gateways = node.get_default_gateways()
        ipv4_gateway_set, ipv6_gateway_set = False, False

        def set_gateway_ip(iface, subnet, ret, ipv4_set, ipv6_set):
            ip_family = subnet.get_ipnetwork().version
            if ip_family == IPADDRESS_FAMILY.IPv4 and ipv4_set:
                return (ret, ipv4_set, ipv6_set)
            elif ip_family == IPADDRESS_FAMILY.IPv6 and ipv6_set:
                return (ret, ipv4_set, ipv6_set)
            for gateway in gateways:
                if gateway is None:
                    continue
                if isinstance(gateway, list):
                    continue
                iface_id, subnet_id, gateway_ip = gateway
                if (
                    iface_id == iface.id
                    and subnet_id == subnet.id
                    and gateway_ip == subnet.gateway_ip
                ):
                    ret += "    gateway: %s\n" % gateway_ip
                    if ip_family == IPADDRESS_FAMILY.IPv4:
                        ipv4_set = True
                    elif ip_family == IPADDRESS_FAMILY.IPv6:
                        ipv6_set = True
            return (ret, ipv4_set, ipv6_set)

        def get_param_value(value):
            if isinstance(value, (bytes, str)):
                return value
            elif isinstance(value, bool):
                return 1 if value else 0
            else:
                return value

        def set_interface_params(iface, ret):
            if iface.params:
                for key, value in iface.params.items():
                    if (
                        not key.startswith("bond_")
                        and not key.startswith("bridge_")
                        and key != "mtu"
                    ):
                        ret += "  %s: %s\n" % (key, get_param_value(value))
            ret += "  mtu: %s\n" % iface.get_effective_mtu()
            return ret

        def is_link_up(addresses):
            if len(addresses) == 0:
                return True
            elif len(addresses) == 1:
                address = addresses[0]
                if (
                    address.alloc_type == IPADDRESS_TYPE.STICKY
                    and not address.ip
                ):
                    return True
            return False

        ret = ""
        for iface in interfaces:
            self.assertIn(iface.type, ["physical", "bond", "vlan", "bridge"])
            fmt_dict = {"name": iface.name, "mac": str(iface.mac_address)}
            if iface.type == "physical":
                ret += self.IFACE_CONFIG % fmt_dict
            elif iface.type == "bridge":
                ret += self.BRIDGE_CONFIG % fmt_dict
                for parent in iface.parents.order_by("name"):
                    ret += "  - %s\n" % parent.name
                ret += "  params:\n"
                if iface.params:
                    for key, value in iface.params.items():
                        if key.startswith("bridge_"):
                            ret += "    %s: %s\n" % (
                                key,
                                get_param_value(value),
                            )
            elif iface.type == "bond":
                ret += self.BOND_CONFIG % fmt_dict
                for parent in iface.parents.order_by("name"):
                    ret += "  - %s\n" % parent.name
                ret += "  params:\n"
                if iface.params:
                    for key, value in iface.params.items():
                        if key.startswith("bond_"):
                            key = key.replace("bond_", "bond-")
                            ret += "    %s: %s\n" % (
                                key,
                                get_param_value(value),
                            )
            elif iface.type == "vlan":
                fmt_dict["parent"] = iface.parents.first().get_name()
                fmt_dict["vlan_id"] = iface.vlan.vid
                ret += self.VLAN_CONFIG % fmt_dict
            ret = set_interface_params(iface, ret)
            addresses = iface.ip_addresses.exclude(
                alloc_type__in=[IPADDRESS_TYPE.DISCOVERED, IPADDRESS_TYPE.DHCP]
            ).order_by("id")
            ret += "  subnets:\n"
            if is_link_up(addresses):
                ret += "  - type: manual\n"
            else:
                for address in addresses:
                    subnet = address.subnet
                    if subnet is not None:
                        subnet_len = subnet.cidr.split("/")[1]
                        ret += "  - address: %s/%s\n" % (
                            str(address.ip),
                            subnet_len,
                        )
                        ret += "    type: static\n"
                        (
                            ret,
                            ipv4_gateway_set,
                            ipv6_gateway_set,
                        ) = set_gateway_ip(
                            iface,
                            subnet,
                            ret,
                            ipv4_gateway_set,
                            ipv6_gateway_set,
                        )
                        if subnet.dns_servers is not None:
                            ret += "    dns_nameservers:\n"
                            for dns_server in subnet.dns_servers:
                                ret += "    - %s\n" % dns_server
                            ret += "    dns_search:\n"
                            for domain in self.get_dns_search_list(
                                node.domain.name
                            ):
                                ret += "    - %s\n" % domain
                dhcp_types = set()
                for dhcp_ip in iface.ip_addresses.filter(
                    alloc_type=IPADDRESS_TYPE.DHCP
                ):
                    if dhcp_ip.subnet is None:
                        dhcp_types.add(4)
                        dhcp_types.add(6)
                    else:
                        dhcp_types.add(dhcp_ip.subnet.get_ipnetwork().version)
                if dhcp_types == set([4, 6]):
                    ret += "  - type: dhcp\n"
                elif dhcp_types == set([4]):
                    ret += "  - type: dhcp4\n"
                elif dhcp_types == set([6]):
                    ret += "  - type: dhcp6\n"
        return ret

    def collect_dns_config(self, node, ipv4=True, ipv6=True):
        config = "- type: nameserver\n  address: %s\n  search:\n" % (
            repr(node.get_default_dns_servers(ipv4=ipv4, ipv6=ipv6))
        )
        domain_name = node.domain.name
        dns_searches = self.get_dns_search_list(domain_name)
        for dns_name in dns_searches:
            config += "   - %s\n" % dns_name
        return config

    def get_dns_search_list(self, domain_name):
        return [domain_name] + [
            name
            for name in sorted(get_dns_search_paths())
            if name != domain_name
        ]


class TestSingleAddrFamilyLayout(MAASServerTestCase, AssertNetworkConfigMixin):

    scenarios = (("ipv4", {"version": 4}), ("ipv6", {"version": 6}))

    def test_renders_expected_output(self):
        # Force it to have a domain name in the middle of two others.  This
        # will confirm that sorting is working correctly.
        factory.make_Domain("aaa")
        domain = factory.make_Domain("bbb")
        factory.make_Domain("ccc")
        subnet = factory.make_Subnet(version=self.version)
        node = factory.make_Node_with_Interface_on_Subnet(
            interface_count=2, subnet=subnet, domain=domain
        )
        for iface in node.interface_set.filter(enabled=True):
            factory.make_StaticIPAddress(
                interface=iface, subnet=iface.vlan.subnet_set.first()
            )
            iface.params = {
                "mtu": random.randint(600, 1400),
                "accept_ra": factory.pick_bool(),
                "autoconf": factory.pick_bool(),
            }
            iface.save()
        extra_interface = node.interface_set.all()[1]
        sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=None,
            interface=extra_interface,
        )
        sip.subnet = None
        sip.save()
        factory.make_Interface(node=node)
        net_config = self.collect_interface_config(node)
        net_config += self.collect_dns_config(
            node, ipv4=(self.version == 4), ipv6=(self.version == 6)
        )
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestSimpleNetworkLayout(MAASServerTestCase, AssertNetworkConfigMixin):
    def test__renders_expected_output(self):
        # Force it to have a domain name in the middle of two others.  This
        # will confirm that sorting is working correctly.
        factory.make_Domain("aaa")
        domain = factory.make_Domain("bbb")
        factory.make_Domain("ccc")
        node = factory.make_Node_with_Interface_on_Subnet(
            interface_count=2, domain=domain
        )
        for iface in node.interface_set.filter(enabled=True):
            factory.make_StaticIPAddress(
                interface=iface, subnet=iface.vlan.subnet_set.first()
            )
            iface.params = {
                "mtu": random.randint(600, 1400),
                "accept_ra": factory.pick_bool(),
                "autoconf": factory.pick_bool(),
            }
            iface.save()
        extra_interface = node.interface_set.all()[1]
        sip = factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY,
            ip="",
            subnet=None,
            interface=extra_interface,
        )
        sip.subnet = None
        sip.save()
        factory.make_Interface(node=node)
        net_config = self.collect_interface_config(node)
        net_config += self.collect_dns_config(node)
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestBondNetworkLayout(MAASServerTestCase, AssertNetworkConfigMixin):
    def test__renders_expected_output(self):
        node = factory.make_Node_with_Interface_on_Subnet(interface_count=2)
        interfaces = list(node.interface_set.all())
        vlan = node.interface_set.first().vlan
        bond_iface = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            node=node,
            vlan=vlan,
            parents=interfaces,
        )
        bond_iface.params = {"bond_mode": "balance-rr"}
        bond_iface.save()
        factory.make_StaticIPAddress(
            interface=bond_iface,
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=bond_iface.vlan.subnet_set.first(),
        )
        net_config = self.collect_interface_config(node, filter="physical")
        net_config += self.collect_interface_config(node, filter="bond")
        net_config += self.collect_dns_config(node)
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestVLANNetworkLayout(MAASServerTestCase, AssertNetworkConfigMixin):
    def test__renders_expected_output(self):
        node = factory.make_Node_with_Interface_on_Subnet(interface_count=1)
        interfaces = node.interface_set.all()
        vlan_iface = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, node=node, parents=interfaces
        )
        subnet = factory.make_Subnet(vlan=vlan_iface.vlan)
        factory.make_StaticIPAddress(interface=vlan_iface, subnet=subnet)
        net_config = self.collect_interface_config(node, filter="physical")
        net_config += self.collect_interface_config(node, filter="vlan")
        net_config += self.collect_dns_config(node)
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestVLANOnBondNetworkLayout(
    MAASServerTestCase, AssertNetworkConfigMixin
):
    def test__renders_expected_output(self):
        node = factory.make_Node_with_Interface_on_Subnet(interface_count=2)
        phys_ifaces = list(node.interface_set.all())
        phys_vlan = node.interface_set.first().vlan
        bond_iface = factory.make_Interface(
            iftype=INTERFACE_TYPE.BOND,
            node=node,
            vlan=phys_vlan,
            parents=phys_ifaces,
        )
        bond_iface.params = {"bond_mode": "balance-rr"}
        bond_iface.save()
        vlan_iface = factory.make_Interface(
            iftype=INTERFACE_TYPE.VLAN, node=node, parents=[bond_iface]
        )
        subnet = factory.make_Subnet(vlan=vlan_iface.vlan)
        factory.make_StaticIPAddress(interface=vlan_iface, subnet=subnet)
        net_config = self.collect_interface_config(node, filter="physical")
        net_config += self.collect_interface_config(node, filter="bond")
        net_config += self.collect_interface_config(node, filter="vlan")
        net_config += self.collect_dns_config(node)
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestDHCPNetworkLayout(MAASServerTestCase, AssertNetworkConfigMixin):

    scenarios = (("ipv4", {"ip_version": 4}), ("ipv6", {"ip_version": 6}))

    def test__dhcp_configurations_rendered(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            ip_version=self.ip_version
        )
        iface = node.interface_set.first()
        subnet = iface.vlan.subnet_set.first()
        factory.make_StaticIPAddress(
            ip=None,
            alloc_type=IPADDRESS_TYPE.DHCP,
            interface=iface,
            subnet=subnet,
        )
        # Patch resolve_hostname() to return the appropriate network version
        # IP address for MAAS hostname.
        resolve_hostname = self.patch(
            maasserver.server_address, "resolve_hostname"
        )
        if self.ip_version == 4:
            resolve_hostname.return_value = {IPAddress("127.0.0.1")}
        else:
            resolve_hostname.return_value = {IPAddress("::1")}
        config = compose_curtin_network_config(node)
        config_yaml = yaml.safe_load(config[0])
        self.assertThat(
            config_yaml["network"]["config"][0]["subnets"][0]["type"],
            Equals("dhcp" + str(IPNetwork(subnet.cidr).version)),
        )


class TestBridgeNetworkLayout(MAASServerTestCase, AssertNetworkConfigMixin):
    def test__renders_expected_output(self):
        node = factory.make_Node_with_Interface_on_Subnet()
        boot_interface = node.get_boot_interface()
        vlan = boot_interface.vlan
        mac_address = factory.make_mac_address()
        bridge_iface = factory.make_Interface(
            iftype=INTERFACE_TYPE.BRIDGE,
            node=node,
            vlan=vlan,
            parents=[boot_interface],
            mac_address=mac_address,
        )
        bridge_iface.params = {"bridge_fd": 0, "bridge_stp": True}
        bridge_iface.save()
        factory.make_StaticIPAddress(
            interface=bridge_iface,
            alloc_type=IPADDRESS_TYPE.STICKY,
            subnet=bridge_iface.vlan.subnet_set.first(),
        )
        net_config = self.collect_interface_config(node, filter="physical")
        net_config += self.collect_interface_config(node, filter="bridge")
        net_config += self.collect_dns_config(node)
        config = compose_curtin_network_config(node)
        self.assertNetworkConfig(net_config, config)


class TestNetplan(MAASServerTestCase):
    def _render_netplan_dict(self, node, source_routing=False):
        return NodeNetworkConfiguration(
            node, version=2, source_routing=source_routing
        ).config

    def _render_v1_dict(self, node):
        return NodeNetworkConfiguration(node, version=1).config

    def get_line_number(self, content, match):
        for line_num, line in enumerate(content.splitlines()):
            if match in line:
                return line_num

    def test__yaml_output_is_ordered(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            parents=[eth0, eth1],
            mac_address=eth0.mac_address,
        )
        [output] = compose_curtin_network_config(node, version=2)
        ethernets_line = self.get_line_number(output, "ethernets:")
        bonds_line = self.get_line_number(output, "bonds:")
        self.assertTrue(
            ethernets_line < bonds_line, "ethernets: must come before bonds:"
        )

    def test__single_ethernet_interface(self):
        node = factory.make_Node()
        factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__multiple_ethernet_interfaces(self):
        node = factory.make_Node()
        factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__bond(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            parents=[eth0, eth1],
            mac_address=eth0.mac_address,
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bonds",
                        {
                            "bond0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": "00:01:02:03:04:05",
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__non_lacp_bond_with_params(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            parents=[eth0, eth1],
            mac_address="03:01:02:03:04:05",
            params={
                "bond_mode": "active-backup",
                "bond_lacp_rate": "fast",
                "bond_xmit_hash_policy": "layer2",
                "bond_num_grat_arp": 3,
            },
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bonds",
                        {
                            "bond0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "parameters": {
                                    "mode": "active-backup",
                                    "transmit-hash-policy": "layer2",
                                    # XXX Workaround LP: #1827238
                                    "gratuitious-arp": 3,
                                },
                                "macaddress": "03:01:02:03:04:05",
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__lacp_bond_with_params(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            parents=[eth0, eth1],
            mac_address="03:01:02:03:04:05",
            params={
                "bond_mode": "802.3ad",
                "bond_lacp_rate": "fast",
                "bond_xmit_hash_policy": "layer2",
                "bond_num_grat_arp": 3,
            },
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bonds",
                        {
                            "bond0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "parameters": {
                                    "mode": "802.3ad",
                                    "lacp-rate": "fast",
                                    "transmit-hash-policy": "layer2",
                                },
                                "macaddress": "03:01:02:03:04:05",
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__active_backup_with_legacy_parameter(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        bond0 = factory.make_Interface(
            INTERFACE_TYPE.BOND,
            node=node,
            name="bond0",
            parents=[eth0, eth1],
            params={
                "bond_mode": "active-backup",
                "bond_lacp_rate": "fast",
                "bond_xmit_hash_policy": "layer2",
                "bond_num_unsol_na": 3,
            },
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bonds",
                        {
                            "bond0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": bond0.mac_address,
                                "parameters": {
                                    "mode": "active-backup",
                                    "transmit-hash-policy": "layer2",
                                    # XXX Workaround LP: #1827238
                                    "gratuitious-arp": 3,
                                },
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__bridge(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, node=node, name="br0", parents=[eth0, eth1]
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bridges",
                        {
                            "br0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": br0.mac_address,
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__bridge_standard_with_params(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            node=node,
            name="br0",
            parents=[eth0, eth1],
            params={
                "bridge_type": BRIDGE_TYPE.STANDARD,
                "bridge_stp": False,
                "bridge_fd": 15,
            },
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bridges",
                        {
                            "br0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": br0.mac_address,
                                "parameters": {
                                    "forward-delay": 15,
                                    "stp": False,
                                },
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))
        # Verify that stp is boolean value not an integer value.
        [output] = compose_curtin_network_config(node, version=2)
        self.assertTrue(
            "stp: false" in output,
            "stp: value must be a boolean not an integer",
        )

    def test__bridge_standard_fallback_with_params(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            node=node,
            name="br0",
            parents=[eth0, eth1],
            params={"bridge_stp": False, "bridge_fd": 15},
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bridges",
                        {
                            "br0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": br0.mac_address,
                                "parameters": {
                                    "forward-delay": 15,
                                    "stp": False,
                                },
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))
        # Verify that stp is boolean value not an integer value.
        [output] = compose_curtin_network_config(node, version=2)
        self.assertTrue(
            "stp: false" in output,
            "stp: value must be a boolean not an integer",
        )

    def test__bridge_ovs_with_params(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE,
            node=node,
            name="br0",
            parents=[eth0, eth1],
            params={
                "bridge_type": BRIDGE_TYPE.OVS,
                "bridge_stp": False,
                "bridge_fd": 15,
            },
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bridges",
                        {
                            "br0": {
                                "interfaces": ["eth0", "eth1"],
                                "mtu": 1500,
                                "macaddress": br0.mac_address,
                                "parameters": {
                                    "forward-delay": 15,
                                    "stp": False,
                                },
                                "renderer": "openvswitch",
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))
        # Verify that stp is boolean value not an integer value.
        [output] = compose_curtin_network_config(node, version=2)
        self.assertTrue(
            "stp: false" in output,
            "stp: value must be a boolean not an integer",
        )

    def test__bridged_bond(self):
        node = factory.make_Node()
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05"
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        bond0 = factory.make_Interface(
            INTERFACE_TYPE.BOND, node=node, name="bond0", parents=[eth0, eth1]
        )
        br0 = factory.make_Interface(
            INTERFACE_TYPE.BRIDGE, node=node, name="br0", parents=[bond0]
        )
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                            },
                        },
                    ),
                    (
                        "bonds",
                        {
                            "bond0": {
                                "interfaces": ["eth0", "eth1"],
                                "macaddress": bond0.mac_address,
                                "mtu": 1500,
                            }
                        },
                    ),
                    (
                        "bridges",
                        {
                            "br0": {
                                "interfaces": ["bond0"],
                                "macaddress": br0.mac_address,
                                "mtu": 1500,
                            }
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))

    def test__multiple_ethernet_interfaces_with_routes(self):
        node = factory.make_Node()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(
            cidr="10.0.0.0/24", gateway_ip="10.0.0.1", dns_servers=[]
        )
        subnet2 = factory.make_Subnet(
            cidr="10.0.1.0/24", gateway_ip="10.0.1.1", dns_servers=[]
        )
        dest_subnet = factory.make_Subnet(cidr="192.168.0.0/24")
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05", vlan=vlan
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        node.boot_interface = eth0
        node.save()
        factory.make_StaticIPAddress(
            interface=eth0,
            subnet=subnet,
            ip="10.0.0.4",
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        factory.make_StaticIPAddress(
            interface=eth1,
            subnet=subnet2,
            ip="10.0.1.4",
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        factory.make_StaticRoute(
            source=subnet,
            gateway_ip="10.0.0.3",
            destination=dest_subnet,
            metric=42,
        )
        factory.make_StaticRoute(
            source=subnet2,
            gateway_ip="10.0.1.3",
            destination=dest_subnet,
            metric=43,
        )
        # Make sure we know when and where the default DNS server will be used.
        get_default_dns_servers_mock = self.patch(
            node, "get_default_dns_servers"
        )
        nameserver_addresses = ["127.0.0.2"]
        get_default_dns_servers_mock.return_value = nameserver_addresses
        domain = Domain.objects.first()
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "gateway4": "10.0.0.1",
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                                "addresses": ["10.0.0.4/24"],
                                "routes": [
                                    {
                                        "to": "192.168.0.0/24",
                                        "via": "10.0.0.3",
                                        "metric": 42,
                                    }
                                ],
                                "nameservers": {
                                    "addresses": nameserver_addresses,
                                    "search": [domain.name],
                                },
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                                "addresses": ["10.0.1.4/24"],
                                "routes": [
                                    {
                                        "to": "192.168.0.0/24",
                                        "via": "10.0.1.3",
                                        "metric": 43,
                                    }
                                ],
                                "nameservers": {
                                    "addresses": nameserver_addresses,
                                    "search": [domain.name],
                                },
                            },
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))
        netplan_with_source_routing = self._render_netplan_dict(
            node, source_routing=True
        )
        expected_netplan_with_source_routing = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "gateway4": "10.0.0.1",
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                                "addresses": ["10.0.0.4/24"],
                                "routes": [
                                    {
                                        "to": "192.168.0.0/24",
                                        "via": "10.0.0.3",
                                        "metric": 42,
                                    }
                                ],
                                "nameservers": {
                                    "addresses": nameserver_addresses,
                                    "search": [domain.name],
                                },
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth1",
                                "addresses": ["10.0.1.4/24"],
                                "routes": [
                                    {
                                        "to": "192.168.0.0/24",
                                        "via": "10.0.1.3",
                                        "metric": 43,
                                    },
                                    {
                                        "to": "0.0.0.0/0",
                                        "via": "10.0.1.1",
                                        "table": 1,
                                    },
                                ],
                                "routing-policy": [
                                    {
                                        "from": "10.0.1.0/24",
                                        "priority": 100,
                                        "table": 1,
                                    },
                                    {
                                        "from": "10.0.1.0/24",
                                        "table": 254,
                                        "to": "10.0.1.0/24",
                                    },
                                ],
                                "nameservers": {
                                    "addresses": nameserver_addresses,
                                    "search": [domain.name],
                                },
                            },
                        },
                    ),
                ]
            )
        }
        self.expectThat(
            netplan_with_source_routing,
            Equals(expected_netplan_with_source_routing),
        )
        v1 = self._render_v1_dict(node)
        expected_v1 = {
            "network": {
                "version": 1,
                "config": [
                    {
                        "id": "eth0",
                        "mac_address": "00:01:02:03:04:05",
                        "mtu": 1500,
                        "name": "eth0",
                        "subnets": [
                            {
                                "address": "10.0.0.4/24",
                                "gateway": "10.0.0.1",
                                "type": "static",
                                "routes": [
                                    {
                                        "destination": "192.168.0.0/24",
                                        "gateway": "10.0.0.3",
                                        "metric": 42,
                                    }
                                ],
                            }
                        ],
                        "type": "physical",
                    },
                    {
                        "id": "eth1",
                        "mac_address": "02:01:02:03:04:05",
                        "mtu": 1500,
                        "name": "eth1",
                        "subnets": [
                            {
                                "address": "10.0.1.4/24",
                                "type": "static",
                                "routes": [
                                    {
                                        "destination": "192.168.0.0/24",
                                        "gateway": "10.0.1.3",
                                        "metric": 43,
                                    }
                                ],
                            }
                        ],
                        "type": "physical",
                    },
                    {
                        "address": ["127.0.0.2"],
                        "search": ["maas"],
                        "type": "nameserver",
                    },
                ],
            }
        }
        self.expectThat(v1, Equals(expected_v1))

    def test__multiple_ethernet_interfaces_with_dns(self):
        node = factory.make_Node()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(
            cidr="10.0.0.0/24", gateway_ip="10.0.0.1", dns_servers=["10.0.0.2"]
        )
        subnet2 = factory.make_Subnet(
            cidr="10.0.1.0/24", gateway_ip="10.0.1.1", dns_servers=["10.0.1.2"]
        )
        eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05", vlan=vlan
        )
        eth1 = factory.make_Interface(
            node=node, name="eth1", mac_address="02:01:02:03:04:05"
        )
        node.boot_interface = eth0
        node.save()
        factory.make_StaticIPAddress(
            interface=eth0,
            subnet=subnet,
            ip="10.0.0.4",
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        factory.make_StaticIPAddress(
            interface=eth1,
            subnet=subnet2,
            ip="10.0.1.4",
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        # Make sure we know when and where the default DNS server will be used.
        get_default_dns_servers_mock = self.patch(
            node, "get_default_dns_servers"
        )
        get_default_dns_servers_mock.return_value = ["127.0.0.2"]
        domain = Domain.objects.first()
        domain.name = "ubuntu.com"
        domain.save()
        domain2 = factory.make_Domain()
        expected_search_list = [domain.name, domain2.name]
        netplan = self._render_netplan_dict(node)
        expected_netplan = {
            "network": OrderedDict(
                [
                    ("version", 2),
                    (
                        "ethernets",
                        {
                            "eth0": {
                                "gateway4": "10.0.0.1",
                                "nameservers": {
                                    "addresses": ["10.0.0.2"],
                                    "search": expected_search_list,
                                },
                                "match": {"macaddress": "00:01:02:03:04:05"},
                                "mtu": 1500,
                                "set-name": "eth0",
                                "addresses": ["10.0.0.4/24"],
                            },
                            "eth1": {
                                "match": {"macaddress": "02:01:02:03:04:05"},
                                "nameservers": {
                                    "addresses": ["10.0.1.2"],
                                    "search": expected_search_list,
                                },
                                "mtu": 1500,
                                "set-name": "eth1",
                                "addresses": ["10.0.1.4/24"],
                            },
                        },
                    ),
                ]
            )
        }
        self.expectThat(netplan, Equals(expected_netplan))
        v1 = self._render_v1_dict(node)
        expected_v1 = {
            "network": {
                "version": 1,
                "config": [
                    {
                        "id": "eth0",
                        "mac_address": "00:01:02:03:04:05",
                        "mtu": 1500,
                        "name": "eth0",
                        "subnets": [
                            {
                                "address": "10.0.0.4/24",
                                "dns_nameservers": ["10.0.0.2"],
                                "dns_search": expected_search_list,
                                "gateway": "10.0.0.1",
                                "type": "static",
                            }
                        ],
                        "type": "physical",
                    },
                    {
                        "id": "eth1",
                        "mac_address": "02:01:02:03:04:05",
                        "mtu": 1500,
                        "name": "eth1",
                        "subnets": [
                            {
                                "address": "10.0.1.4/24",
                                "dns_nameservers": ["10.0.1.2"],
                                "dns_search": expected_search_list,
                                "type": "static",
                            }
                        ],
                        "type": "physical",
                    },
                    {
                        "address": ["127.0.0.2"],
                        "search": expected_search_list,
                        "type": "nameserver",
                    },
                ],
            }
        }
        self.expectThat(v1, Equals(expected_v1))

    def test__ha__default_dns(self):
        node = factory.make_Node()
        mock_get_source_address = self.patch(
            preseed_network_module, "get_source_address"
        )
        mock_get_source_address.return_value = "10.0.0.1"
        vlan = factory.make_VLAN()
        r1 = factory.make_RegionRackController(interface=False)
        mock_get_maas_id = self.patch(server_address_module, "get_maas_id")
        mock_get_maas_id.return_value = r1.system_id
        r2 = factory.make_RegionRackController(interface=False)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, vlan=vlan, node=r2
        )
        subnet = factory.make_Subnet(
            cidr="10.0.0.0/24", gateway_ip="10.0.0.1", dns_servers=[]
        )
        r2_address = factory.make_StaticIPAddress(
            interface=interface,
            subnet=subnet,
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        vlan = factory.make_VLAN()
        factory.make_Subnet(
            cidr="10.0.1.0/24", gateway_ip="10.0.1.1", dns_servers=[]
        )
        node_eth0 = factory.make_Interface(
            node=node, name="eth0", mac_address="00:01:02:03:04:05", vlan=vlan
        )
        node.boot_interface = node_eth0
        node.save()
        factory.make_StaticIPAddress(
            interface=node_eth0,
            subnet=subnet,
            ip="10.0.0.4",
            alloc_type=IPADDRESS_TYPE.STICKY,
        )
        # XXX: the netplan (v2) currently doesn't include default DNS servers.
        # See launchpad bug #1664806.
        v1 = self._render_v1_dict(node)
        expected_v1 = {
            "network": {
                "version": 1,
                "config": [
                    {
                        "id": "eth0",
                        "mac_address": "00:01:02:03:04:05",
                        "mtu": 1500,
                        "name": "eth0",
                        "subnets": [
                            {
                                "address": "10.0.0.4/24",
                                "gateway": "10.0.0.1",
                                "type": "static",
                            }
                        ],
                        "type": "physical",
                    },
                    {
                        "address": [r2_address.ip, "10.0.0.1"],
                        "search": ["maas"],
                        "type": "nameserver",
                    },
                ],
            }
        }
        self.expectThat(v1, Equals(expected_v1))

    def test__commissioning_dhcp_config(self):
        # Verifies dhcp config is given when commissioning has run
        # or just run and no AUTOIP has been acquired.
        subnet = factory.make_Subnet(dns_servers=[])
        subnet_ver = subnet.get_ipnetwork().version
        node = factory.make_Node_with_Interface_on_Subnet(
            subnet=subnet,
            **random.choice(
                [
                    {"status": NODE_STATUS.COMMISSIONING},
                    {
                        "status": NODE_STATUS.TESTING,
                        "previous_status": NODE_STATUS.COMMISSIONING,
                    },
                ]
            )
        )
        node.set_initial_networking_configuration()
        v2 = self._render_netplan_dict(node)
        self.assertDictEqual(
            v2,
            {
                "network": {
                    "version": 2,
                    "ethernets": {
                        node.boot_interface.name: {
                            "mtu": 1500,
                            "match": {
                                "macaddress": str(
                                    node.boot_interface.mac_address
                                )
                            },
                            "set-name": node.boot_interface.name,
                            "gateway%d" % subnet_ver: subnet.gateway_ip,
                            "dhcp%d" % subnet_ver: True,
                        }
                    },
                }
            },
        )


class TestGetNextRoutingTableId(MAASServerTestCase):
    def test__routing_table_index_starts_at_one(self):
        node = factory.make_Node()
        generator = NodeNetworkConfiguration(node)
        table_id = generator.get_next_routing_table_id()
        self.assertThat(table_id, Equals(1))

    def test__raises_IndexError_for_too_many_tables(self):
        node = factory.make_Node()
        generator = NodeNetworkConfiguration(node)
        for _ in range(252):
            generator.get_next_routing_table_id()
        with ExpectedException(IndexError):
            generator.get_next_routing_table_id()
