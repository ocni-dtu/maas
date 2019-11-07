# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for miscellaneous helpers."""

__all__ = []

import threading
from unittest.mock import sentinel
from urllib.parse import urlencode, urljoin

from django.http import HttpRequest
from django.test.client import RequestFactory
from maasserver.models import Config, RegionController
from maasserver.testing.config import RegionConfigurationFixture
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils import (
    absolute_reverse,
    build_absolute_uri,
    find_rack_controller,
    get_default_region_ip,
    get_host_without_port,
    get_local_cluster_UUID,
    get_maas_user_agent,
    get_remote_ip,
    strip_domain,
    synchronised,
)
from maasserver.utils.django_urls import reverse
from maastesting.matchers import IsNonEmptyString
from maastesting.testcase import MAASTestCase
from provisioningserver.testing.config import ClusterConfigurationFixture
from provisioningserver.utils.testing import MAASIDFixture
from provisioningserver.utils.version import get_maas_version_user_agent
from testtools.matchers import Contains, Equals, Not


class TestAbsoluteReverse(MAASServerTestCase):
    def expected_from_maas_url_and_reverse(self, maas_url, reversed_url):
        # We need to remove the leading '/' from the reversed url, or
        # urljoin won't actually join.
        return urljoin(maas_url.rstrip("/") + "/", reversed_url.lstrip("/"))

    def test_absolute_reverse_uses_maas_url_by_default(self):
        maas_url = factory.make_simple_http_url(path="")
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        absolute_url = absolute_reverse("settings")
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reverse("settings")
        )
        self.assertEqual(expected_url, absolute_url)

    def test_absolute_reverse_handles_base_url_without_ending_slash(self):
        maas_url = factory.make_simple_http_url()
        maas_url = maas_url.rstrip("/")
        absolute_url = absolute_reverse("settings", base_url=maas_url)
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reverse("settings")
        )
        self.assertEqual(expected_url, absolute_url)

    def test_absolute_reverse_uses_given_base_url(self):
        maas_url = factory.make_simple_http_url()
        absolute_url = absolute_reverse("settings", base_url=maas_url)
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reverse("settings")
        )
        self.assertEqual(expected_url, absolute_url)

    def test_absolute_reverse_uses_query_string(self):
        maas_url = factory.make_simple_http_url()
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))

        parameters = {factory.make_string(): factory.make_string()}
        absolute_url = absolute_reverse("settings", query=parameters)
        reversed_url = "%s?%s" % (reverse("settings"), urlencode(parameters))
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reversed_url
        )
        self.assertEqual(expected_url, absolute_url)

    def test_absolute_reverse_uses_kwargs(self):
        maas_url = factory.make_simple_http_url()
        user = factory.make_User()
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))
        absolute_url = absolute_reverse(
            "accounts-edit", kwargs={"username": user.username}
        )
        reversed_url = reverse("accounts-edit", args=[user.username])
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reversed_url
        )
        self.assertEqual(expected_url, absolute_url)

    def test_absolute_reverse_uses_args(self):
        maas_url = factory.make_simple_http_url()
        user = factory.make_User()
        self.useFixture(RegionConfigurationFixture(maas_url=maas_url))

        observed_url = absolute_reverse(
            "accounts-edit", kwargs={"username": user.username}
        )

        reversed_url = reverse("accounts-edit", args=[user.username])
        expected_url = self.expected_from_maas_url_and_reverse(
            maas_url, reversed_url
        )
        self.assertEqual(expected_url, observed_url)


class TestBuildAbsoluteURI(MAASTestCase):
    """Tests for `build_absolute_uri`."""

    def setUp(self):
        super(TestBuildAbsoluteURI, self).setUp()
        self.useFixture(RegionConfigurationFixture())

    def make_request(
        self, host="example.com", port=80, script_name="", is_secure=False
    ):
        """Return a :class:`HttpRequest` with the given parameters."""
        request = HttpRequest()
        request.META["SERVER_NAME"] = host
        request.META["SERVER_PORT"] = port
        request.META["SCRIPT_NAME"] = script_name
        request.is_secure = lambda: is_secure
        return request

    def test_simple(self):
        request = self.make_request()
        self.assertEqual(
            "http://example.com/fred", build_absolute_uri(request, "/fred")
        )

    def test_different_port(self):
        request = self.make_request(port=1234)
        self.assertEqual(
            "http://example.com:1234/fred",
            build_absolute_uri(request, "/fred"),
        )

    def test_script_name_is_ignored(self):
        # The given path already includes the script_name, so the
        # script_name passed in the request is not included again.
        request = self.make_request(script_name="/foo/bar")
        self.assertEqual(
            "http://example.com/foo/bar/fred",
            build_absolute_uri(request, "/foo/bar/fred"),
        )

    def test_secure(self):
        request = self.make_request(port=443, is_secure=True)
        self.assertEqual(
            "https://example.com/fred", build_absolute_uri(request, "/fred")
        )

    def test_different_port_and_secure(self):
        request = self.make_request(port=9443, is_secure=True)
        self.assertEqual(
            "https://example.com:9443/fred",
            build_absolute_uri(request, "/fred"),
        )

    def test_preserve_two_leading_slashes(self):
        # Whilst this shouldn't ordinarily happen, two leading slashes in the
        # path should be preserved, and not treated specially.
        request = self.make_request()
        self.assertEqual(
            "http://example.com//foo", build_absolute_uri(request, "//foo")
        )


class TestStripDomain(MAASTestCase):
    def test_strip_domain(self):
        input_and_results = [
            ("name.domain", "name"),
            ("name", "name"),
            ("name.domain.what", "name"),
            ("name..domain", "name"),
        ]
        inputs = [input for input, _ in input_and_results]
        results = [result for _, result in input_and_results]
        self.assertEqual(results, list(map(strip_domain, inputs)))


class TestGetLocalClusterUUID(MAASTestCase):
    def test_get_local_cluster_UUID_returns_None_if_not_set(self):
        self.useFixture(ClusterConfigurationFixture())
        self.assertIsNone(get_local_cluster_UUID())

    def test_get_local_cluster_UUID_returns_cluster_UUID(self):
        uuid = factory.make_UUID()
        self.useFixture(ClusterConfigurationFixture(cluster_uuid=uuid))
        self.assertEqual(uuid, get_local_cluster_UUID())


def make_request(origin_ip, http_host=None):
    """Return a fake HTTP request with the given remote address."""
    if http_host is None:
        return RequestFactory().post("/", REMOTE_ADDR=str(origin_ip))
    else:
        return RequestFactory().post(
            "/", REMOTE_ADDR=str(origin_ip), HTTP_HOST=str(http_host)
        )


class TestFindRackController(MAASServerTestCase):
    def test_returns_None_when_unknown_subnet(self):
        self.assertIsNone(
            find_rack_controller(make_request(factory.make_ip_address()))
        )

    def test_returns_None_when_subnet_is_not_managed(self):
        subnet = factory.make_Subnet()
        self.assertIsNone(
            find_rack_controller(
                make_request(factory.pick_ip_in_Subnet(subnet))
            )
        )

    def test_returns_primary_rack_when_subnet_is_managed(self):
        subnet = factory.make_Subnet()
        rack_controller = factory.make_RackController()
        subnet.vlan.dhcp_on = True
        subnet.vlan.primary_rack = rack_controller
        subnet.vlan.save()
        self.assertEquals(
            rack_controller.system_id,
            find_rack_controller(
                make_request(factory.pick_ip_in_Subnet(subnet))
            ).system_id,
        )


class TestSynchronised(MAASTestCase):
    def test_locks_when_calling(self):
        lock = threading.Lock()

        @synchronised(lock)
        def example_synchronised_function():
            self.assertTrue(lock.locked())
            return sentinel.called

        self.assertFalse(lock.locked())
        self.assertEqual(sentinel.called, example_synchronised_function())
        self.assertFalse(lock.locked())


class TestGetMAASUserAgent(MAASServerTestCase):
    def test_get_maas_user_agent_without_uuid(self):
        user_agent = get_maas_user_agent()
        uuid = Config.objects.get_config("uuid")
        self.assertEqual(uuid, None)
        self.assertThat(user_agent, IsNonEmptyString)
        self.assertThat(user_agent, Not(Contains(uuid)))

    def test_get_maas_user_agent_with_uuid(self):
        region = factory.make_RegionController()
        self.useFixture(MAASIDFixture(region.system_id))
        RegionController.objects.get_or_create_uuid()
        user_agent = get_maas_user_agent()
        composed_user_agent = "%s/%s" % (
            get_maas_version_user_agent(),
            Config.objects.get_config("uuid"),
        )
        self.assertEquals(user_agent, composed_user_agent)


class TestGetHostWithoutPort(MAASTestCase):

    scenarios = (
        ("ipv4", {"host": "127.0.0.1", "expected": "127.0.0.1"}),
        (
            "ipv4-with-port",
            {"host": "127.0.0.1:1234", "expected": "127.0.0.1"},
        ),
        (
            "ipv6",
            {"host": "[2001:db8::1:2:3:4]", "expected": "2001:db8::1:2:3:4"},
        ),
        (
            "ipv6-with-port",
            {"host": "[2001:db8::1]:4567", "expected": "2001:db8::1"},
        ),
        ("dns", {"host": "maas.example.com", "expected": "maas.example.com"}),
    )

    def test__returns_expected_results(self):
        self.assertThat(
            get_host_without_port(self.host), Equals(self.expected)
        )


class TestGetDefaultRegionIP(MAASServerTestCase):
    def test__returns_source_ip_based_on_remote_ip_if_no_Host_header(self):
        # Note: the source IP should resolve to the loopback interface here.
        self.assertThat(
            get_default_region_ip(make_request("127.0.0.2")),
            Equals("127.0.0.1"),
        )

    def test__returns_Host_header_if_available(self):
        self.assertThat(
            get_default_region_ip(make_request("127.0.0.1", "localhost")),
            Equals("localhost"),
        )

    def test__returns_Host_header_if_available_and_strips_port(self):
        self.assertThat(
            get_default_region_ip(make_request("127.0.0.1", "localhost:5240")),
            Equals("localhost"),
        )


class GetRemoteIPTest(MAASTestCase):
    def test__gets_client_ipv4_for_HTTP_X_FORWARDED_FOR(self):
        ip_address = factory.make_ipv4_address()
        request = HttpRequest()
        request.META = {"HTTP_X_FORWARDED_FOR": ip_address}
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__gets_client_ipv6_for_HTTP_X_FORWARDED_FOR(self):
        ip_address = factory.make_ipv6_address()
        request = HttpRequest()
        request.META = {"HTTP_X_FORWARDED_FOR": ip_address}
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__gets_client_ip_for_X_FORWARDED_FOR_with_proxies(self):
        ip_address = factory.make_ipv4_address()
        proxy1 = factory.make_ipv4_address()
        proxy2 = factory.make_ipv4_address()
        request = HttpRequest()
        request.META = {
            "HTTP_X_FORWARDED_FOR": "%s, %s, %s" % (ip_address, proxy1, proxy2)
        }
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__gets_client_ipv4_for_REMOTE_ADDR(self):
        ip_address = factory.make_ipv4_address()
        request = HttpRequest()
        request.META = {"REMOTE_ADDR": ip_address}
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__gets_client_ipv6_for_REMOTE_ADDR(self):
        ip_address = factory.make_ipv6_address()
        request = HttpRequest()
        request.META = {"REMOTE_ADDR": ip_address}
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__fallsback_to_REMOTE_ADDR_for_invalid_X_FORWARDED_FOR(self):
        ip_address = factory.make_ipv4_address()
        request = HttpRequest()
        request.META = {
            "HTTP_X_FORWARDED_FOR": factory.make_name("garbage ip"),
            "REMOTE_ADDR": ip_address,
        }
        self.assertEquals(ip_address, get_remote_ip(request))

    def test__returns_None_for_invalid_ip(self):
        ip_address = factory.make_name("garbage ip")
        request = HttpRequest()
        request.META = {"REMOTE_ADDR": ip_address}
        self.assertIsNone(get_remote_ip(request))

    def test__returns_None_empty_META(self):
        request = HttpRequest()
        request.META = {}
        self.assertIsNone(get_remote_ip(request))
