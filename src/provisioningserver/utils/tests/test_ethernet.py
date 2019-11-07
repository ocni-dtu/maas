# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ``provisioningserver.utils.ethernet``."""

__all__ = []

import random

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver.utils.ethernet import Ethernet, ETHERTYPE
from provisioningserver.utils.network import hex_str_to_bytes
from testtools.matchers import Equals


def make_ethernet_packet(
    dst_mac="ff:ff:ff:ff:ff:ff",
    src_mac="01:02:03:04:05:06",
    ethertype=ETHERTYPE.ARP,
    vid=None,
    payload=b"",
):
    """Construct an Ethernet packet using the specified values.

    If the specified `vid` is not None, it is interpreted as an integer VID,
    and the appropriate Ethertype fields are adjusted.
    """
    # Basic Ethernet header is (destination, source, ethertype)
    ethernet_packet = (
        hex_str_to_bytes(dst_mac)
        + hex_str_to_bytes(src_mac)
        +
        # If a VID is defined, use the 802.1q Ethertype instead...
        (hex_str_to_bytes("8100") if vid is not None else ethertype)
    )
    if vid is not None:
        ethernet_packet = (
            ethernet_packet
            + bytes.fromhex("%04x" % vid)
            +
            # ... and place the payload Ethertype in the 802.1q header.
            ethertype
        )
    ethernet_packet = ethernet_packet + payload
    return ethernet_packet


class TestEthernet(MAASTestCase):
    def test__is_valid_returns_false_for_truncated_non_vlan(self):
        src_mac = factory.make_mac_address()
        dst_mac = factory.make_mac_address()
        ethertype = ETHERTYPE.ARP
        payload = factory.make_bytes(48)
        packet = make_ethernet_packet(
            dst_mac=dst_mac,
            src_mac=src_mac,
            ethertype=ethertype,
            payload=payload,
        )
        packet = packet[0:13]
        eth = Ethernet(packet)
        self.assertThat(eth.is_valid(), Equals(False))

    def test__is_valid_returns_false_for_truncated_vlan(self):
        src_mac = factory.make_mac_address()
        dst_mac = factory.make_mac_address()
        ethertype = ETHERTYPE.ARP
        payload = factory.make_bytes(48)
        vid = random.randrange(4095)
        packet = make_ethernet_packet(
            dst_mac=dst_mac,
            src_mac=src_mac,
            ethertype=ethertype,
            payload=payload,
            vid=vid,
        )
        packet = packet[0:15]
        eth = Ethernet(packet)
        self.assertThat(eth.is_valid(), Equals(False))

    def test__parses_non_vlan(self):
        src_mac = factory.make_mac_address()
        dst_mac = factory.make_mac_address()
        ethertype = ETHERTYPE.ARP
        payload = factory.make_bytes(48)
        eth = Ethernet(
            make_ethernet_packet(
                dst_mac=dst_mac,
                src_mac=src_mac,
                ethertype=ethertype,
                payload=payload,
            )
        )
        self.assertThat(eth.dst_mac, Equals(hex_str_to_bytes(dst_mac)))
        self.assertThat(eth.src_mac, Equals(hex_str_to_bytes(src_mac)))
        self.assertThat(eth.ethertype, Equals(ethertype))
        self.assertThat(eth.payload, Equals(payload))
        self.assertThat(eth.is_valid(), Equals(True))

    def test__parses_vlan(self):
        src_mac = factory.make_mac_address()
        dst_mac = factory.make_mac_address()
        ethertype = ETHERTYPE.ARP
        payload = factory.make_bytes(48)
        vid = random.randrange(4095)
        eth = Ethernet(
            make_ethernet_packet(
                dst_mac=dst_mac,
                src_mac=src_mac,
                ethertype=ethertype,
                payload=payload,
                vid=vid,
            )
        )
        self.assertThat(eth.dst_mac, Equals(hex_str_to_bytes(dst_mac)))
        self.assertThat(eth.src_mac, Equals(hex_str_to_bytes(src_mac)))
        self.assertThat(eth.ethertype, Equals(ethertype))
        self.assertThat(eth.payload, Equals(payload))
        self.assertThat(eth.vid, Equals(vid))
        self.assertThat(eth.is_valid(), Equals(True))
