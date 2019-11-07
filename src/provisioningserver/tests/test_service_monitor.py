# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the DHCPv4 and DHCPv6 service driver."""

__all__ = []

from maastesting.testcase import MAASTestCase
from provisioningserver.service_monitor import (
    DHCPv4Service,
    DHCPv6Service,
    DNSServiceOnRack,
    NTPServiceOnRack,
    ProxyServiceOnRack,
    service_monitor,
    SyslogServiceOnRack,
)


class TestDHCPv4Service(MAASTestCase):
    def test_name(self):
        service = DHCPv4Service()
        self.assertEqual("dhcpd", service.name)

    def test_service_name(self):
        service = DHCPv4Service()
        self.assertEqual("maas-dhcpd", service.service_name)


class TestDHCPv6Service(MAASTestCase):
    def test_name(self):
        service = DHCPv6Service()
        self.assertEqual("dhcpd6", service.name)

    def test_service_name(self):
        service = DHCPv6Service()
        self.assertEqual("maas-dhcpd6", service.service_name)


class TestNTPServiceOnRack(MAASTestCase):
    def test_name_and_service_name(self):
        ntp = NTPServiceOnRack()
        self.assertEqual("chrony", ntp.service_name)
        self.assertEqual("ntp_rack", ntp.name)


class TestDNSServiceOnRack(MAASTestCase):
    def test_name_and_service_name(self):
        dns = DNSServiceOnRack()
        self.assertEqual("bind9", dns.service_name)
        self.assertEqual("dns_rack", dns.name)


class TestProxyServiceOnRack(MAASTestCase):
    def test_name_and_service_name(self):
        proxy = ProxyServiceOnRack()
        self.assertEqual("maas-proxy", proxy.service_name)
        self.assertEqual("proxy", proxy.snap_service_name)
        self.assertEqual("proxy_rack", proxy.name)


class TestSyslogServiceOnRack(MAASTestCase):
    def test_name_and_service_name(self):
        syslog = SyslogServiceOnRack()
        self.assertEqual("maas-syslog", syslog.service_name)
        self.assertEqual("syslog", syslog.snap_service_name)
        self.assertEqual("syslog_rack", syslog.name)


class TestGlobalServiceMonitor(MAASTestCase):
    def test__includes_all_services(self):
        self.assertItemsEqual(
            [
                "http",
                "dhcpd",
                "dhcpd6",
                "dns_rack",
                "ntp_rack",
                "proxy_rack",
                "syslog_rack",
            ],
            service_monitor._services.keys(),
        )
