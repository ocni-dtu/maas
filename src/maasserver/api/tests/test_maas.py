# Copyright 2015-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for maas endpoint in the API."""

__all__ = []

import http.client
import json
from operator import itemgetter
import random

from django.conf import settings
from maasserver.forms.settings import CONFIG_ITEMS_KEYS
from maasserver.models import PackageRepository
from maasserver.models.config import Config, DEFAULT_CONFIG
from maasserver.testing.api import APITestCase
from maasserver.testing.factory import factory
from maasserver.testing.osystems import (
    make_osystem_with_releases,
    make_usable_osystem,
    patch_usable_osystems,
)
from maasserver.utils.django_urls import reverse
from maastesting.matchers import DocTestMatches
from maastesting.testcase import MAASTestCase
from testtools.content import text_content
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    MatchesAll,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)

# Names forbidden for use via the Web API.
FORBIDDEN_NAMES = {
    "omapi_key",
    "rpc_region_certificate",
    "rpc_shared_secret",
    "commissioning_osystem",
    "active_discovery_last_scan",
    "uuid",
    "external_auth_url",
    "external_auth_domain",
    "external_auth_user",
    "external_auth_key",
    "external_auth_admin_group",
    "macaroon_private_key",
    "rbac_url",
}


class TestForbiddenNames(MAASTestCase):
    def test_forbidden_names(self):
        # The difference between the set of possible configuration keys and
        # those permitted via the Web API is small but important to security.
        self.assertThat(
            set(DEFAULT_CONFIG).difference(CONFIG_ITEMS_KEYS),
            Equals(FORBIDDEN_NAMES),
        )


class MAASHandlerAPITest(APITestCase.ForUser):
    def test_get_config_default_distro_series(self):
        default_distro_series = factory.make_name("distro_series")
        Config.objects.set_config(
            "default_distro_series", default_distro_series
        )
        response = self.client.get(
            reverse("maas_handler"),
            {"op": "get_config", "name": "default_distro_series"},
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        expected = '"%s"' % default_distro_series
        self.assertEqual(
            expected.encode(settings.DEFAULT_CHARSET), response.content
        )

    def test_set_config_default_distro_series(self):
        self.become_admin()
        osystem = make_usable_osystem(self)
        Config.objects.set_config("default_osystem", osystem["name"])
        selected_release = osystem["releases"][0]["name"]
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "default_distro_series",
                "value": selected_release,
            },
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        self.assertEqual(
            selected_release,
            Config.objects.get_config("default_distro_series"),
        )

    def test_set_config_only_default_osystem_are_valid_for_distro_series(self):
        self.become_admin()
        default_osystem = make_osystem_with_releases(self)
        other_osystem = make_osystem_with_releases(self)
        patch_usable_osystems(self, [default_osystem, other_osystem])
        Config.objects.set_config("default_osystem", default_osystem["name"])
        invalid_release = other_osystem["releases"][0]["name"]
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "default_distro_series",
                "value": invalid_release,
            },
        )
        self.assertEqual(
            http.client.BAD_REQUEST, response.status_code, response.content
        )

    def assertInvalidConfigurationSetting(self, name, response):
        self.addDetail(
            "Response for op={get,set}_config&name=%s" % name,
            text_content(
                response.serialize().decode(settings.DEFAULT_CHARSET)
            ),
        )
        self.expectThat(
            response,
            MatchesAll(
                # An HTTP 400 response,
                MatchesStructure(status_code=Equals(http.client.BAD_REQUEST)),
                # with a JSON body,
                AfterPreprocessing(
                    itemgetter("Content-Type"), Equals("application/json")
                ),
                # containing a serialised ValidationError.
                AfterPreprocessing(
                    lambda response: json.loads(
                        response.content.decode(settings.DEFAULT_CHARSET)
                    ),
                    MatchesDict(
                        {
                            name: MatchesListwise(
                                [
                                    DocTestMatches(
                                        name
                                        + " is not a valid config setting "
                                        "(valid settings are: ...)."
                                    )
                                ]
                            )
                        }
                    ),
                ),
                first_only=True,
            ),
        )

    def test_get_config_forbidden_config_items(self):
        for name in FORBIDDEN_NAMES:
            response = self.client.get(
                reverse("maas_handler"), {"op": "get_config", "name": name}
            )
            self.assertInvalidConfigurationSetting(name, response)

    def test_set_config_forbidden_config_items(self):
        self.become_admin()
        for name in FORBIDDEN_NAMES:
            response = self.client.post(
                reverse("maas_handler"),
                {
                    "op": "set_config",
                    "name": name,
                    "value": factory.make_name("nonsense"),
                },
            )
            self.assertInvalidConfigurationSetting(name, response)

    def test_get_config_ntp_server_alias_for_ntp_servers(self):
        ntp_servers = factory.make_hostname() + " " + factory.make_hostname()
        Config.objects.set_config("ntp_servers", ntp_servers)
        response = self.client.get(
            reverse("maas_handler"), {"op": "get_config", "name": "ntp_server"}
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(status_code=Equals(http.client.OK)),
                # with a JSON body,
                AfterPreprocessing(
                    itemgetter("Content-Type"), Equals("application/json")
                ),
                # containing the ntp_servers setting.
                AfterPreprocessing(
                    lambda response: json.loads(
                        response.content.decode(settings.DEFAULT_CHARSET)
                    ),
                    Equals(ntp_servers),
                ),
            ),
        )

    def test_set_config_ntp_server_alias_for_ntp_servers(self):
        self.become_admin()
        ntp_servers = factory.make_hostname() + " " + factory.make_hostname()
        response = self.client.post(
            reverse("maas_handler"),
            {"op": "set_config", "name": "ntp_server", "value": ntp_servers},
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(
                    status_code=Equals(http.client.OK), content=Equals(b"OK")
                )
            ),
        )
        self.assertThat(
            Config.objects.get_config("ntp_servers"), Equals(ntp_servers)
        )

    def test_get_main_archive_overrides_to_package_repository(self):
        PackageRepository.objects.all().delete()
        main_url = factory.make_url(scheme="http")
        factory.make_PackageRepository(
            url=main_url, default=True, arches=["i386", "amd64"]
        )
        response = self.client.get(
            reverse("maas_handler"),
            {"op": "get_config", "name": "main_archive"},
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(status_code=Equals(http.client.OK)),
                # with a JSON body,
                AfterPreprocessing(
                    itemgetter("Content-Type"), Equals("application/json")
                ),
                # containing the main_archive setting.
                AfterPreprocessing(
                    lambda response: json.loads(
                        response.content.decode(settings.DEFAULT_CHARSET)
                    ),
                    Equals(main_url),
                ),
            ),
        )

    def test_get_ports_archive_overrides_to_package_repository(self):
        PackageRepository.objects.all().delete()
        ports_url = factory.make_url(scheme="http")
        factory.make_PackageRepository(
            url=ports_url, default=True, arches=["arm64", "armhf", "powerpc"]
        )
        response = self.client.get(
            reverse("maas_handler"),
            {"op": "get_config", "name": "ports_archive"},
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(status_code=Equals(http.client.OK)),
                # with a JSON body,
                AfterPreprocessing(
                    itemgetter("Content-Type"), Equals("application/json")
                ),
                # containing the main_archive setting.
                AfterPreprocessing(
                    lambda response: json.loads(
                        response.content.decode(settings.DEFAULT_CHARSET)
                    ),
                    Equals(ports_url),
                ),
            ),
        )

    def test_set_main_archive_overrides_to_package_repository(self):
        self.become_admin()
        main_archive = factory.make_url(scheme="http")
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "main_archive",
                "value": main_archive,
            },
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(
                    status_code=Equals(http.client.OK), content=Equals(b"OK")
                )
            ),
        )
        self.assertThat(
            PackageRepository.get_main_archive().url, Equals(main_archive)
        )

    def test_set_ports_archive_overrides_to_package_repository(self):
        self.become_admin()
        ports_archive = factory.make_url(scheme="http")
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "ports_archive",
                "value": ports_archive,
            },
        )
        self.assertThat(
            response,
            MatchesAll(
                # An HTTP 200 response,
                MatchesStructure(
                    status_code=Equals(http.client.OK), content=Equals(b"OK")
                )
            ),
        )
        self.assertThat(
            PackageRepository.get_ports_archive().url, Equals(ports_archive)
        )

    def test_set_config_use_peer_proxy(self):
        self.become_admin()
        response = self.client.post(
            reverse("maas_handler"),
            {"op": "set_config", "name": "use_peer_proxy", "value": True},
        )
        self.assertEqual(http.client.OK, response.status_code)
        self.assertTrue(Config.objects.get_config("use_peer_proxy"))

    def test_set_config_prefer_v4_proxy(self):
        self.become_admin()
        response = self.client.post(
            reverse("maas_handler"),
            {"op": "set_config", "name": "prefer_v4_proxy", "value": True},
        )
        self.assertEqual(http.client.OK, response.status_code)
        self.assertTrue(Config.objects.get_config("prefer_v4_proxy"))

    def test_set_config_boot_images_no_proxy(self):
        self.become_admin()
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "boot_images_no_proxy",
                "value": True,
            },
        )
        self.assertEqual(http.client.OK, response.status_code)
        self.assertTrue(Config.objects.get_config("boot_images_no_proxy"))

    def test_get_config_maas_internal_domain(self):
        internal_domain = factory.make_name("internal")
        Config.objects.set_config("maas_internal_domain", internal_domain)
        response = self.client.get(
            reverse("maas_handler"),
            {"op": "get_config", "name": "maas_internal_domain"},
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        expected = '"%s"' % internal_domain
        self.assertEqual(
            expected.encode(settings.DEFAULT_CHARSET), response.content
        )

    def test_set_config_maas_internal_domain(self):
        self.become_admin()
        internal_domain = factory.make_name("internal")
        response = self.client.post(
            reverse("maas_handler"),
            {
                "op": "set_config",
                "name": "maas_internal_domain",
                "value": internal_domain,
            },
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        self.assertEqual(
            internal_domain, Config.objects.get_config("maas_internal_domain")
        )


class MAASHandlerAPITestForProxyPort(APITestCase.ForUser):

    scenarios = [
        ("valid-port", {"port": random.randint(5300, 65535), "valid": True}),
        (
            "invalid-port_maas-reserved-range",
            {"port": random.randint(5240, 5270), "valid": False},
        ),
        (
            "invalid-port_system-services",
            {"port": random.randint(0, 1023), "valid": False},
        ),
        (
            "invalid-port_out-of-range",
            {"port": random.randint(65536, 70000), "valid": False},
        ),
    ]

    def test_set_config_maas_proxy_port(self):
        self.become_admin()
        port = self.port
        response = self.client.post(
            reverse("maas_handler"),
            {"op": "set_config", "name": "maas_proxy_port", "value": port},
        )
        if self.valid:
            self.assertEqual(http.client.OK, response.status_code)
            self.assertEqual(
                port, Config.objects.get_config("maas_proxy_port")
            )
        else:
            self.assertEqual(
                http.client.BAD_REQUEST, response.status_code, response.content
            )
