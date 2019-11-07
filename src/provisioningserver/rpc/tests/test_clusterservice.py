# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the cluster's RPC implementation."""

__all__ = []

from hashlib import sha256
from hmac import HMAC
from itertools import product
import json
import os.path
import platform
import random
from random import randint
import socket
from unittest.mock import ANY, call, MagicMock, Mock, sentinel
from urllib.parse import urlparse

from apiclient.creds import convert_tuple_to_string
from apiclient.utils import ascii_url
from maastesting.factory import factory
from maastesting.matchers import (
    IsUnfiredDeferred,
    MockAnyCall,
    MockCalledOnceWith,
    MockCalledWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from maastesting.twisted import (
    always_fail_with,
    always_succeed_with,
    extract_result,
    TwistedLoggerFixture,
)
from netaddr import IPNetwork
from provisioningserver import concurrency
from provisioningserver.boot import tftppath
from provisioningserver.boot.tests.test_tftppath import make_osystem
from provisioningserver.dhcp.testing.config import (
    DHCPConfigNameResolutionDisabled,
    fix_shared_networks_failover,
    make_failover_peer_config,
    make_host,
    make_interface,
    make_shared_network,
    make_shared_network_v1,
)
from provisioningserver.drivers.nos.registry import NOSDriverRegistry
from provisioningserver.drivers.osystem import (
    OperatingSystem,
    OperatingSystemRegistry,
)
from provisioningserver.drivers.pod import (
    DiscoveredMachine,
    DiscoveredPod,
    DiscoveredPodHints,
    RequestedMachine,
    RequestedMachineBlockDevice,
    RequestedMachineInterface,
)
from provisioningserver.drivers.power import PowerError
from provisioningserver.drivers.power.registry import PowerDriverRegistry
from provisioningserver.path import get_data_path
from provisioningserver.rpc import (
    boot_images,
    cluster,
    clusterservice,
    common,
    dhcp,
    exceptions,
    getRegionClient,
    osystems as osystems_rpc_module,
    pods,
    power as power_module,
    region,
    tags,
)
from provisioningserver.rpc.clusterservice import (
    Cluster,
    ClusterClient,
    ClusterClientCheckerService,
    ClusterClientService,
    executeScanNetworksSubprocess,
    get_scan_all_networks_args,
    spawnProcessAndNullifyStdout,
)
from provisioningserver.rpc.interfaces import IConnection
from provisioningserver.rpc.osystems import gen_operating_systems
from provisioningserver.rpc.testing import (
    are_valid_tls_parameters,
    call_responder,
    MockLiveClusterToRegionRPCFixture,
)
from provisioningserver.rpc.testing.doubles import DummyConnection, StubOS
from provisioningserver.security import set_shared_secret_on_filesystem
from provisioningserver.service_monitor import service_monitor
from provisioningserver.testing.config import ClusterConfigurationFixture
from provisioningserver.utils.env import set_maas_id
from provisioningserver.utils.fs import get_maas_common_command, NamedLock
from provisioningserver.utils.network import get_all_interfaces_definition
from provisioningserver.utils.shell import ExternalProcessError
from provisioningserver.utils.twisted import (
    makeDeferredWithProcessProtocol,
    pause,
)
from provisioningserver.utils.version import get_maas_version
from testtools import ExpectedException
from testtools.matchers import (
    Equals,
    HasLength,
    Is,
    IsInstance,
    KeysEqual,
    MatchesAll,
    MatchesDict,
    MatchesListwise,
    MatchesStructure,
)
from twisted import web
from twisted.application.internet import TimerService
from twisted.internet import error, reactor
from twisted.internet.defer import Deferred, fail, inlineCallbacks, succeed
from twisted.internet.endpoints import TCP6ClientEndpoint
from twisted.internet.error import ConnectionClosed
from twisted.internet.task import Clock
from twisted.protocols import amp
from twisted.python.failure import Failure
from twisted.python.threadable import isInIOThread
from twisted.test.proto_helpers import StringTransportWithDisconnection
from twisted.web.client import Headers
from zope.interface.verify import verifyObject


class TestClusterProtocol_Identify(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_identify_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.Identify.commandName)
        self.assertIsNotNone(responder)

    def test_identify_reports_system_id(self):
        system_id = factory.make_name("id")
        self.patch(clusterservice, "get_maas_id").return_value = system_id
        d = call_responder(Cluster(), cluster.Identify, {})

        def check(response):
            self.assertEqual({"ident": system_id}, response)

        return d.addCallback(check)


class TestClusterProtocol_Authenticate(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_authenticate_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.Authenticate.commandName)
        self.assertIsNotNone(responder)

    def test_authenticate_calculates_digest_with_salt(self):
        message = factory.make_bytes()
        secret = factory.make_bytes()
        set_shared_secret_on_filesystem(secret)

        args = {"message": message}
        d = call_responder(Cluster(), cluster.Authenticate, args)
        response = extract_result(d)
        digest = response["digest"]
        salt = response["salt"]

        self.assertThat(salt, HasLength(16))
        expected_digest = HMAC(secret, message + salt, sha256).digest()
        self.assertEqual(expected_digest, digest)


class TestClusterProtocol_StartTLS(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_StartTLS_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(amp.StartTLS.commandName)
        self.assertIsNotNone(responder)

    def test_get_tls_parameters_returns_parameters(self):
        # get_tls_parameters() is the underlying responder function.
        # However, locateResponder() returns a closure, so we have to
        # side-step it.
        protocol = Cluster()
        cls, func = protocol._commandDispatch[amp.StartTLS.commandName]
        self.assertThat(func(protocol), are_valid_tls_parameters)

    def test_StartTLS_returns_nothing(self):
        # The StartTLS command does some funky things - see _TLSBox and
        # _LocalArgument for an idea - so the parameters returned from
        # get_tls_parameters() - the registered responder - don't end up
        # travelling over the wire as part of an AMP message. However,
        # the responder is not aware of this, and is called just like
        # any other.
        d = call_responder(Cluster(), amp.StartTLS, {})

        def check(response):
            self.assertEqual({}, response)

        return d.addCallback(check)


class TestClusterProtocol_ListBootImages_and_ListBootImagesV2(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = (
        ("ListBootImages", {"rpc_call": cluster.ListBootImages}),
        ("ListBootImagesV2", {"rpc_call": cluster.ListBootImagesV2}),
    )

    def test_list_boot_images_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(self.rpc_call.commandName)
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_list_boot_images_can_be_called(self):
        self.useFixture(ClusterConfigurationFixture())
        self.patch(boot_images, "CACHED_BOOT_IMAGES", None)
        list_boot_images = self.patch(tftppath, "list_boot_images")
        list_boot_images.return_value = []

        response = yield call_responder(Cluster(), self.rpc_call, {})

        self.assertEqual({"images": []}, response)

    @inlineCallbacks
    def test_list_boot_images_with_things_to_report(self):
        # tftppath.list_boot_images()'s return value matches the
        # response schema that ListBootImages declares, and is
        # serialised correctly.

        # Example boot image definitions.
        osystems = "ubuntu", "centos"
        archs = "i386", "amd64"
        subarchs = "generic", "special"
        releases = "precise", "trusty"
        labels = "beta-1", "release"
        purposes = "commissioning", "install", "xinstall"

        # Populate a TFTP file tree with a variety of subdirectories.
        tftpdir = self.make_dir()
        current_dir = os.path.join(tftpdir, "current")
        os.makedirs(current_dir)
        for options in product(osystems, archs, subarchs, releases, labels):
            os.makedirs(os.path.join(current_dir, *options))
            make_osystem(self, options[0], purposes)

        self.useFixture(
            ClusterConfigurationFixture(
                tftp_root=os.path.join(tftpdir, "current")
            )
        )
        self.patch(boot_images, "CACHED_BOOT_IMAGES", None)

        expected_images = [
            {
                "osystem": osystem,
                "architecture": arch,
                "subarchitecture": subarch,
                "release": release,
                "label": label,
                "purpose": purpose,
            }
            for osystem, arch, subarch, release, label, purpose in product(
                osystems, archs, subarchs, releases, labels, purposes
            )
        ]
        for expected_image in expected_images:
            if expected_image["purpose"] == "xinstall":
                if expected_image["osystem"] == "ubuntu":
                    expected_image["xinstall_path"] = "squashfs"
                    expected_image["xinstall_type"] = "squashfs"
                else:
                    expected_image["xinstall_path"] = "root-tgz"
                    expected_image["xinstall_type"] = "tgz"
            else:
                expected_image["xinstall_path"] = ""
                expected_image["xinstall_type"] = ""

        response = yield call_responder(Cluster(), self.rpc_call, {})

        self.assertThat(response, KeysEqual("images"))
        self.assertItemsEqual(expected_images, response["images"])


class TestClusterProtocol_ImportBootImages(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_import_boot_images_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.ImportBootImages.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_import_boot_images_can_be_called(self):
        self.patch(clusterservice, "import_boot_images")

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        response = yield call_responder(
            conn_cluster, cluster.ImportBootImages, {"sources": []}
        )
        self.assertEqual({}, response)

    @inlineCallbacks
    def test_import_boot_images_calls_import_boot_images_with_sources(self):
        import_boot_images = self.patch(clusterservice, "import_boot_images")

        sources = [
            {
                "url": factory.make_url(),
                "keyring_data": b"",
                "selections": [
                    {
                        "os": "ubuntu",
                        "release": "trusty",
                        "arches": ["amd64"],
                        "subarches": ["generic"],
                        "labels": ["release"],
                    }
                ],
            }
        ]

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        yield call_responder(
            conn_cluster, cluster.ImportBootImages, {"sources": sources}
        )

        self.assertThat(
            import_boot_images,
            MockCalledOnceWith(
                sources,
                conn_cluster.service.maas_url,
                http_proxy=None,
                https_proxy=None,
            ),
        )

    @inlineCallbacks
    def test_import_boot_images_calls_import_boot_images_with_proxies(self):
        import_boot_images = self.patch(clusterservice, "import_boot_images")

        proxy = "http://%s.example.com" % factory.make_name("proxy")
        parsed_proxy = urlparse(proxy)

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        yield call_responder(
            conn_cluster,
            cluster.ImportBootImages,
            {
                "sources": [],
                "http_proxy": parsed_proxy,
                "https_proxy": parsed_proxy,
            },
        )

        self.assertThat(
            import_boot_images,
            MockCalledOnceWith(
                [],
                conn_cluster.service.maas_url,
                http_proxy=proxy,
                https_proxy=proxy,
            ),
        )


class TestClusterProtocol_IsImportBootImagesRunning(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_import_boot_images_running_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.IsImportBootImagesRunning.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_is_import_boot_images_running_returns_False(self):
        mock_is_running = self.patch(
            clusterservice, "is_import_boot_images_running"
        )
        mock_is_running.return_value = False
        response = yield call_responder(
            Cluster(), cluster.IsImportBootImagesRunning, {}
        )
        self.assertEqual({"running": False}, response)

    @inlineCallbacks
    def test_is_import_boot_images_running_returns_True(self):
        mock_is_running = self.patch(
            clusterservice, "is_import_boot_images_running"
        )
        mock_is_running.return_value = True
        response = yield call_responder(
            Cluster(), cluster.IsImportBootImagesRunning, {}
        )
        self.assertEqual({"running": True}, response)


class TestClusterProtocol_DescribePowerTypes(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_describe_power_types_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.DescribePowerTypes.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_describe_power_types_returns_jsonized_schema(self):

        response = yield call_responder(
            Cluster(), cluster.DescribePowerTypes, {}
        )

        self.assertThat(response, KeysEqual("power_types"))
        self.assertItemsEqual(
            PowerDriverRegistry.get_schema(detect_missing_packages=False),
            response["power_types"],
        )


class TestClusterProtocol_DescribeNOSTypes(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_describe_nos_types_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.DescribeNOSTypes.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_describe_nos_types_returns_jsonized_schema(self):

        response = yield call_responder(
            Cluster(), cluster.DescribeNOSTypes, {}
        )

        self.assertThat(response, KeysEqual("nos_types"))
        self.assertItemsEqual(
            NOSDriverRegistry.get_schema(), response["nos_types"]
        )


def make_inert_client_service():
    service = ClusterClientService(Clock())
    # ClusterClientService's superclass, TimerService, creates a
    # LoopingCall with now=True. We neuter it here to allow
    # observation of the behaviour of _update_interval() for
    # example.
    service.call = (lambda: None, (), {})
    return service


class TestClusterClientService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def fakeAgentResponse(self, data):
        def mock_body_producer(code, phrase, defer):
            defer.callback(data)
            return Mock()

        self.patch(clusterservice, "_ReadBodyProtocol", mock_body_producer)
        mock_agent = MagicMock()
        response = MagicMock()
        mock_agent.request.return_value = succeed(response)
        self.patch(clusterservice, "Agent").return_value = mock_agent

    def test_init_sets_appropriate_instance_attributes(self):
        service = ClusterClientService(sentinel.reactor)
        self.assertThat(service, IsInstance(TimerService))
        self.assertThat(service.clock, Is(sentinel.reactor))

    def test__get_config_rpc_info_urls(self):
        maas_urls = [factory.make_simple_http_url() for _ in range(3)]
        self.useFixture(ClusterConfigurationFixture(maas_url=maas_urls))
        service = ClusterClientService(reactor)
        observed_urls = service._get_config_rpc_info_urls()
        self.assertThat(observed_urls, Equals(maas_urls))

    def test__get_saved_rpc_info_urls(self):
        saved_urls = [factory.make_simple_http_url() for _ in range(3)]
        service = ClusterClientService(reactor)
        with open(service._get_saved_rpc_info_path(), "w") as stream:
            for url in saved_urls:
                stream.write("%s\n" % url)
        observed_urls = service._get_saved_rpc_info_urls()
        self.assertThat(observed_urls, Equals(saved_urls))

    def test_update_saved_rpc_info_state(self):
        service = ClusterClientService(reactor)
        ipv4client = ClusterClient(("1.1.1.1", 1111), "host1:pid=1", service)
        ipv6client = ClusterClient(("::ffff", 2222), "host2:pid=2", service)
        ipv6mapped = ClusterClient(
            ("::ffff:3.3.3.3", 3333), "host3:pid=3", service
        )
        hostclient = ClusterClient(
            ("example.com", 4444), "host4:pid=4", service
        )

        # Fake some connections.
        service.connections = {
            ipv4client.eventloop: ipv4client,
            ipv6client.eventloop: ipv6client,
            ipv6mapped.eventloop: ipv6mapped,
            hostclient.eventloop: hostclient,
        }

        # Update the RPC state to the filesystem and info cache.
        self.assertThat(service._rpc_info_state, Is(None))
        service._update_saved_rpc_info_state()

        # Ensure that the info state is set.
        self.assertThat(
            service._rpc_info_state,
            Equals(
                {
                    client.address[0]
                    for _, client in service.connections.items()
                }
            ),
        )

        # Check that the written rpc state is valid.
        self.assertThat(
            service._get_saved_rpc_info_urls(),
            Equals(
                [
                    "http://1.1.1.1:5240/MAAS",
                    "http://[::ffff]:5240/MAAS",
                    "http://3.3.3.3:5240/MAAS",
                    "http://example.com:5240/MAAS",
                ]
            ),
        )

    @inlineCallbacks
    def test__build_rpc_info_urls(self):
        # Because this actually will try to resolve the URL's in the test we
        # keep them to localhost so it works on all systems.
        maas_urls = ["http://127.0.0.1:5240/" for _ in range(3)]
        expected_urls = [
            ([b"http://127.0.0.1:5240/rpc/"], "http://127.0.0.1:5240/")
            for url in maas_urls
        ]
        service = ClusterClientService(reactor)
        observed_urls = yield service._build_rpc_info_urls(maas_urls)
        self.assertThat(observed_urls, Equals(expected_urls))

    def test__doUpdate_connect_503_error_is_logged_tersely(self):
        mock_agent = MagicMock()
        mock_agent.request.return_value = fail(web.error.Error("503"))
        self.patch(clusterservice, "Agent").return_value = mock_agent

        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.return_value = succeed(
            [([b"http://[::ffff:127.0.0.1]/MAAS"], "http://127.0.0.1/MAAS")]
        )

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCalledOnceWith(
                b"GET",
                ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
            ),
        )
        dump = logger.dump()
        self.assertIn("Region is not advertising RPC endpoints.", dump)

    def test__doUpdate_makes_parallel_requests(self):
        mock_agent = MagicMock()
        mock_agent.request.return_value = always_fail_with(
            web.error.Error("503")
        )
        self.patch(clusterservice, "Agent").return_value = mock_agent

        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _get_config_rpc_info_urls = self.patch(
            service, "_get_config_rpc_info_urls"
        )
        _get_config_rpc_info_urls.return_value = [
            "http://127.0.0.1/MAAS",
            "http://127.0.0.1/MAAS",
        ]
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.return_value = succeed(
            [
                ([b"http://[::ffff:127.0.0.1]/MAAS"], "http://127.0.0.1/MAAS"),
                ([b"http://[::ffff:127.0.0.1]/MAAS"], "http://127.0.0.1/MAAS"),
            ]
        )

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCallsMatch(
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
            ),
        )
        dump = logger.dump()
        self.assertIn(
            "Failed to contact region. (While requesting RPC info at "
            "http://127.0.0.1/MAAS, http://127.0.0.1/MAAS)",
            dump,
        )

    def test__doUpdate_makes_parallel_with_serial_requests(self):
        mock_agent = MagicMock()
        mock_agent.request.return_value = always_fail_with(
            web.error.Error("503")
        )
        self.patch(clusterservice, "Agent").return_value = mock_agent

        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _get_config_rpc_info_urls = self.patch(
            service, "_get_config_rpc_info_urls"
        )
        _get_config_rpc_info_urls.return_value = [
            "http://127.0.0.1/MAAS",
            "http://127.0.0.1/MAAS",
        ]
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.return_value = succeed(
            [
                (
                    [
                        b"http://[::ffff:127.0.0.1]/MAAS",
                        b"http://127.0.0.1/MAAS",
                    ],
                    "http://127.0.0.1/MAAS",
                ),
                (
                    [
                        b"http://[::ffff:127.0.0.1]/MAAS",
                        b"http://127.0.0.1/MAAS",
                    ],
                    "http://127.0.0.1/MAAS",
                ),
            ]
        )

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCallsMatch(
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://127.0.0.1/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://127.0.0.1/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
            ),
        )
        dump = logger.dump()
        self.assertIn(
            "Failed to contact region. (While requesting RPC info at "
            "http://127.0.0.1/MAAS, http://127.0.0.1/MAAS)",
            dump,
        )

    def test__doUpdate_falls_back_to_rpc_info_state(self):
        mock_agent = MagicMock()
        mock_agent.request.return_value = always_fail_with(
            web.error.Error("503")
        )
        self.patch(clusterservice, "Agent").return_value = mock_agent

        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _get_config_rpc_info_urls = self.patch(
            service, "_get_config_rpc_info_urls"
        )
        _get_config_rpc_info_urls.return_value = [
            "http://127.0.0.1/MAAS",
            "http://127.0.0.1/MAAS",
        ]
        _get_saved_rpc_info_urls = self.patch(
            service, "_get_saved_rpc_info_urls"
        )
        _get_saved_rpc_info_urls.return_value = [
            "http://127.0.0.1/MAAS",
            "http://127.0.0.1/MAAS",
        ]
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.side_effect = [
            succeed(
                [
                    (
                        [b"http://[::ffff:127.0.0.1]/MAAS"],
                        "http://127.0.0.1/MAAS",
                    ),
                    (
                        [b"http://[::ffff:127.0.0.1]/MAAS"],
                        "http://127.0.0.1/MAAS",
                    ),
                ]
            ),
            succeed(
                [
                    (
                        [b"http://[::ffff:127.0.0.1]/MAAS"],
                        "http://127.0.0.1/MAAS",
                    ),
                    (
                        [b"http://[::ffff:127.0.0.1]/MAAS"],
                        "http://127.0.0.1/MAAS",
                    ),
                ]
            ),
        ]

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCallsMatch(
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
                call(
                    b"GET",
                    ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                    Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
                ),
            ),
        )
        dump = logger.dump()
        self.assertIn(
            "Failed to contact region. (While requesting RPC info at "
            "http://127.0.0.1/MAAS, http://127.0.0.1/MAAS)",
            dump,
        )

    def test_failed_update_is_logged(self):
        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _doUpdate = self.patch(service, "_doUpdate")
        _doUpdate.side_effect = error.ConnectionRefusedError()

        # Starting the service causes the first update to be performed, which
        # will fail because of above.
        service.startService()
        self.assertThat(_doUpdate, MockCalledOnceWith())

        dump = logger.dump()
        self.assertIn("Connection was refused by other side.", dump)

    def test_update_connect_error_is_logged_tersely(self):
        mock_agent = MagicMock()
        mock_agent.request.side_effect = error.ConnectionRefusedError()
        self.patch(clusterservice, "Agent").return_value = mock_agent

        logger = self.useFixture(TwistedLoggerFixture())

        service = ClusterClientService(Clock())
        _get_config_rpc_info_urls = self.patch(
            service, "_get_config_rpc_info_urls"
        )
        _get_config_rpc_info_urls.return_value = ["http://127.0.0.1/MAAS"]
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.return_value = succeed(
            [([b"http://[::ffff:127.0.0.1]/MAAS"], "http://127.0.0.1/MAAS")]
        )

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCalledOnceWith(
                b"GET",
                ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                Headers({"User-Agent": [ANY], "Host": ["127.0.0.1"]}),
            ),
        )
        dump = logger.dump()
        self.assertIn(
            "Region not available: Connection was refused by other side.", dump
        )
        self.assertIn("While requesting RPC info at", dump)

    def test_update_connect_includes_host(self):
        # Regression test for LP:1792462
        mock_agent = MagicMock()
        mock_agent.request.side_effect = error.ConnectionRefusedError()
        self.patch(clusterservice, "Agent").return_value = mock_agent

        service = ClusterClientService(Clock())
        fqdn = "%s.example.com" % factory.make_hostname()
        _get_config_rpc_info_urls = self.patch(
            service, "_get_config_rpc_info_urls"
        )
        _get_config_rpc_info_urls.return_value = ["http://%s/MAAS" % fqdn]
        _build_rpc_info_urls = self.patch(service, "_build_rpc_info_urls")
        _build_rpc_info_urls.return_value = succeed(
            [([b"http://[::ffff:127.0.0.1]/MAAS"], "http://%s/MAAS" % fqdn)]
        )

        # Starting the service causes the first update to be performed.
        service.startService()

        self.assertThat(
            mock_agent.request,
            MockCalledOnceWith(
                b"GET",
                ascii_url("http://[::ffff:127.0.0.1]/MAAS"),
                Headers({"User-Agent": [ANY], "Host": [fqdn]}),
            ),
        )

    # The following represents an example response from the RPC info
    # view in maasserver. Event-loops listen on ephemeral ports, and
    # it's up to the RPC info view to direct clients to them.
    example_rpc_info_view_response = json.dumps(
        {
            "eventloops": {
                # An event-loop in pid 1001 on host1. This host has two
                # configured IP addresses, 1.1.1.1 and 1.1.1.2.
                "host1:pid=1001": [
                    ("::ffff:1.1.1.1", 1111),
                    ("::ffff:1.1.1.2", 2222),
                ],
                # An event-loop in pid 2002 on host1. This host has two
                # configured IP addresses, 1.1.1.1 and 1.1.1.2.
                "host1:pid=2002": [
                    ("::ffff:1.1.1.1", 3333),
                    ("::ffff:1.1.1.2", 4444),
                ],
                # An event-loop in pid 3003 on host2. This host has one
                # configured IP address, 2.2.2.2.
                "host2:pid=3003": [("::ffff:2.2.2.2", 5555)],
            }
        }
    ).encode("ascii")

    def test__doUpdate_calls__update_connections(self):
        maas_url = "http://localhost/%s/" % factory.make_name("path")
        self.useFixture(ClusterConfigurationFixture(maas_url=maas_url))
        self.patch_autospec(socket, "getaddrinfo").return_value = (
            None,
            None,
            None,
            None,
            ("::ffff:127.0.0.1", 80, 0, 1),
        )
        self.fakeAgentResponse(self.example_rpc_info_view_response)
        service = ClusterClientService(Clock())
        _update_connections = self.patch(service, "_update_connections")
        service.startService()
        self.assertThat(
            _update_connections,
            MockCalledOnceWith(
                {
                    "host2:pid=3003": [["::ffff:2.2.2.2", 5555]],
                    "host1:pid=2002": [
                        ["::ffff:1.1.1.1", 3333],
                        ["::ffff:1.1.1.2", 4444],
                    ],
                    "host1:pid=1001": [
                        ["::ffff:1.1.1.1", 1111],
                        ["::ffff:1.1.1.2", 2222],
                    ],
                }
            ),
        )

    @inlineCallbacks
    def test__update_connections_initially(self):
        service = ClusterClientService(Clock())
        mock_client = Mock()
        _make_connection = self.patch(service, "_make_connection")
        _make_connection.side_effect = lambda *args: succeed(mock_client)
        _drop_connection = self.patch(service, "_drop_connection")

        info = json.loads(self.example_rpc_info_view_response.decode("ascii"))
        yield service._update_connections(info["eventloops"])

        _make_connection_expected = [
            call("host1:pid=1001", ("::ffff:1.1.1.1", 1111)),
            call("host1:pid=2002", ("::ffff:1.1.1.1", 3333)),
            call("host2:pid=3003", ("::ffff:2.2.2.2", 5555)),
        ]
        self.assertItemsEqual(
            _make_connection_expected, _make_connection.call_args_list
        )
        self.assertEquals(
            {
                "host1:pid=1001": mock_client,
                "host1:pid=2002": mock_client,
                "host2:pid=3003": mock_client,
            },
            service.try_connections,
        )

        self.assertEqual([], _drop_connection.mock_calls)

    @inlineCallbacks
    def test__update_connections_logs_fully_connected(self):
        service = ClusterClientService(Clock())
        eventloops = {
            "region1:123": [("::ffff:127.0.0.1", 1234)],
            "region1:124": [("::ffff:127.0.0.1", 1235)],
            "region2:123": [("::ffff:127.0.0.2", 1234)],
            "region2:124": [("::ffff:127.0.0.2", 1235)],
        }
        for eventloop, addresses in eventloops.items():
            for address in addresses:
                client = Mock()
                client.address = address
                service.connections[eventloop] = client

        logger = self.useFixture(TwistedLoggerFixture())

        yield service._update_connections(eventloops)
        # Second call should not add it to the log.
        yield service._update_connections(eventloops)

        self.assertEqual(
            "Fully connected to all 4 event-loops on all 2 region "
            "controllers (region1, region2).",
            logger.dump(),
        )

    @inlineCallbacks
    def test__update_connections_connect_error_is_logged_tersely(self):
        service = ClusterClientService(Clock())
        _make_connection = self.patch(service, "_make_connection")
        _make_connection.side_effect = error.ConnectionRefusedError()

        logger = self.useFixture(TwistedLoggerFixture())

        eventloops = {"an-event-loop": [("127.0.0.1", 1234)]}
        yield service._update_connections(eventloops)

        self.assertThat(
            _make_connection,
            MockCalledOnceWith("an-event-loop", ("::ffff:127.0.0.1", 1234)),
        )

        self.assertEqual(
            "Making connections to event-loops: an-event-loop\n"
            "---\n"
            "Event-loop an-event-loop (::ffff:127.0.0.1:1234): Connection "
            "was refused by other side.",
            logger.dump(),
        )

    @inlineCallbacks
    def test__update_connections_unknown_error_is_logged_with_stack(self):
        service = ClusterClientService(Clock())
        _make_connection = self.patch(service, "_make_connection")
        _make_connection.side_effect = RuntimeError("Something went wrong.")

        logger = self.useFixture(TwistedLoggerFixture())

        eventloops = {"an-event-loop": [("127.0.0.1", 1234)]}
        yield service._update_connections(eventloops)

        self.assertThat(
            _make_connection,
            MockCalledOnceWith("an-event-loop", ("::ffff:127.0.0.1", 1234)),
        )

        self.assertDocTestMatches(
            """\
            Making connections to event-loops: an-event-loop
            ---
            Failure with event-loop an-event-loop (::ffff:127.0.0.1:1234)
            Traceback (most recent call last):
            ...
            builtins.RuntimeError: Something went wrong.
            """,
            logger.dump(),
        )

    def test__update_connections_when_there_are_existing_connections(self):
        service = ClusterClientService(Clock())
        _make_connection = self.patch(service, "_make_connection")
        _drop_connection = self.patch(service, "_drop_connection")

        host1client = ClusterClient(
            ("::ffff:1.1.1.1", 1111), "host1:pid=1", service
        )
        host2client = ClusterClient(
            ("::ffff:2.2.2.2", 2222), "host2:pid=2", service
        )
        host3client = ClusterClient(
            ("::ffff:3.3.3.3", 3333), "host3:pid=3", service
        )

        # Fake some connections.
        service.connections = {
            host1client.eventloop: host1client,
            host2client.eventloop: host2client,
        }

        # Request a new set of connections that overlaps with the
        # existing connections.
        service._update_connections(
            {
                host1client.eventloop: [host1client.address],
                host3client.eventloop: [host3client.address],
            }
        )

        # A connection is made for host3's event-loop, and the
        # connection to host2's event-loop is dropped.
        self.assertThat(
            _make_connection,
            MockCalledOnceWith(host3client.eventloop, host3client.address),
        )
        self.assertThat(_drop_connection, MockCalledWith(host2client))

    @inlineCallbacks
    def test__update_only_updates_interval_when_eventloops_are_unknown(self):
        service = ClusterClientService(Clock())
        self.patch_autospec(service, "_get_config_rpc_info_urls")
        self.patch_autospec(service, "_build_rpc_info_urls")
        self.patch_autospec(service, "_parallel_fetch_rpc_info")
        self.patch_autospec(service, "_update_connections")
        # Return urls from _get_config_rpc_info_urls and _build_rpc_info_urls.
        service._get_config_rpc_info_urls.return_value = [
            "http://127.0.0.1/MAAS"
        ]
        service._build_rpc_info_urls.return_value = succeed(
            [([b"http://[::ffff:127.0.0.1]/MAAS"], "http://127.0.0.1/MAAS")]
        )
        # Return None instead of a list of event-loop endpoints. This is the
        # response that the region will give when the advertising service is
        # not running.
        service._parallel_fetch_rpc_info.return_value = succeed((None, None))
        # Set the step to a bogus value so we can see it change.
        service.step = 999

        logger = self.useFixture(TwistedLoggerFixture())

        yield service.startService()

        self.assertThat(service._update_connections, MockNotCalled())
        self.assertThat(service.step, Equals(service.INTERVAL_LOW))
        self.assertEqual(
            "Region is not advertising RPC endpoints. (While requesting RPC"
            " info at http://127.0.0.1/MAAS)",
            logger.dump(),
        )

    def test__make_connection(self):
        service = ClusterClientService(Clock())
        connectProtocol = self.patch(clusterservice, "connectProtocol")
        service._make_connection("an-event-loop", ("a.example.com", 1111))
        self.assertThat(connectProtocol.call_args_list, HasLength(1))
        self.assertThat(
            connectProtocol.call_args_list[0][0],
            MatchesListwise(
                (
                    # First argument is an IPv4 TCP client endpoint
                    # specification.
                    MatchesAll(
                        IsInstance(TCP6ClientEndpoint),
                        MatchesStructure.byEquality(
                            _reactor=service.clock,
                            _host="a.example.com",
                            _port=1111,
                        ),
                    ),
                    # Second argument is a ClusterClient instance, the
                    # protocol to use for the connection.
                    MatchesAll(
                        IsInstance(clusterservice.ClusterClient),
                        MatchesStructure.byEquality(
                            address=("a.example.com", 1111),
                            eventloop="an-event-loop",
                            service=service,
                        ),
                    ),
                )
            ),
        )

    def test__drop_connection(self):
        connection = Mock()
        service = make_inert_client_service()
        service.startService()
        service._drop_connection(connection)
        self.assertThat(
            connection.transport.loseConnection, MockCalledOnceWith()
        )

    def test__add_connection_removes_from_try_connections(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        connection.address = (":::ffff", 2222)
        service.try_connections[endpoint] = connection
        service.add_connection(endpoint, connection)
        self.assertThat(service.try_connections, Equals({}))

    def test__add_connection_adds_to_connections(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        connection.address = (":::ffff", 2222)
        service.add_connection(endpoint, connection)
        self.assertThat(service.connections, Equals({endpoint: connection}))

    def test__add_connection_calls__update_saved_rpc_info_state(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        connection.address = (":::ffff", 2222)
        self.patch_autospec(service, "_update_saved_rpc_info_state")
        service.add_connection(endpoint, connection)
        self.assertThat(
            service._update_saved_rpc_info_state, MockCalledOnceWith()
        )

    def test__remove_connection_removes_from_try_connections(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        service.try_connections[endpoint] = connection
        service.remove_connection(endpoint, connection)
        self.assertThat(service.try_connections, Equals({}))

    def test__remove_connection_removes_from_connections(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        service.connections[endpoint] = connection
        service.remove_connection(endpoint, connection)
        self.assertThat(service.connections, Equals({}))

    def test__remove_connection_lowers_recheck_interval(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        service.connections[endpoint] = connection
        service.remove_connection(endpoint, connection)
        self.assertEquals(service.step, service.INTERVAL_LOW)

    def test__remove_connection_stops_both_dhcpd_and_dhcpd6(self):
        service = make_inert_client_service()
        service.startService()
        endpoint = Mock()
        connection = Mock()
        service.connections[endpoint] = connection

        # Enable both dhcpd and dhcpd6.
        service_monitor.getServiceByName("dhcpd").on()
        service_monitor.getServiceByName("dhcpd6").on()
        mock_ensureServices = self.patch(service_monitor, "ensureServices")
        service.remove_connection(endpoint, connection)

        self.assertFalse(service_monitor.getServiceByName("dhcpd").is_on())
        self.assertFalse(service_monitor.getServiceByName("dhcpd").is_on())
        self.assertThat(mock_ensureServices, MockCalledOnceWith())

    def test_getClient(self):
        service = ClusterClientService(Clock())
        service.connections = {
            sentinel.eventloop01: DummyConnection(),
            sentinel.eventloop02: DummyConnection(),
            sentinel.eventloop03: DummyConnection(),
        }
        self.assertIn(
            service.getClient(),
            {common.Client(conn) for conn in service.connections.values()},
        )

    def test_getClient_when_there_are_no_connections(self):
        service = ClusterClientService(Clock())
        service.connections = {}
        self.assertRaises(exceptions.NoConnectionsAvailable, service.getClient)

    @inlineCallbacks
    def test_getClientNow_returns_current_connection(self):
        service = ClusterClientService(Clock())
        service.connections = {
            sentinel.eventloop01: DummyConnection(),
            sentinel.eventloop02: DummyConnection(),
            sentinel.eventloop03: DummyConnection(),
        }
        client = yield service.getClientNow()
        self.assertIn(
            client,
            {common.Client(conn) for conn in service.connections.values()},
        )

    @inlineCallbacks
    def test_getClientNow_calls__tryUpdate_when_there_are_no_connections(self):
        service = ClusterClientService(Clock())
        service.connections = {}

        def addConnections():
            service.connections = {
                sentinel.eventloop01: DummyConnection(),
                sentinel.eventloop02: DummyConnection(),
                sentinel.eventloop03: DummyConnection(),
            }
            return succeed(None)

        self.patch(service, "_tryUpdate").side_effect = addConnections
        client = yield service.getClientNow()
        self.assertIn(
            client,
            {common.Client(conn) for conn in service.connections.values()},
        )

    def test_getClientNow_raises_exception_when_no_clients(self):
        service = ClusterClientService(Clock())
        service.connections = {}

        self.patch(service, "_tryUpdate").return_value = succeed(None)
        d = service.getClientNow()
        d.addCallback(lambda _: self.fail("Errback should have been called."))
        d.addErrback(
            lambda failure: self.assertIsInstance(
                failure.value, exceptions.NoConnectionsAvailable
            )
        )
        return d

    def test__tryUpdate_prevents_concurrent_calls_to__doUpdate(self):
        service = ClusterClientService(Clock())

        d_doUpdate_1, d_doUpdate_2 = Deferred(), Deferred()
        _doUpdate = self.patch(service, "_doUpdate")
        _doUpdate.side_effect = [d_doUpdate_1, d_doUpdate_2]

        # Try updating a couple of times concurrently.
        d_tryUpdate_1 = service._tryUpdate()
        d_tryUpdate_2 = service._tryUpdate()
        # _doUpdate completes and returns `done`.
        d_doUpdate_1.callback(sentinel.done1)
        # Both _tryUpdate calls yield the same result.
        self.assertThat(extract_result(d_tryUpdate_1), Is(sentinel.done1))
        self.assertThat(extract_result(d_tryUpdate_2), Is(sentinel.done1))
        # _doUpdate was called only once.
        self.assertThat(_doUpdate, MockCalledOnceWith())

        # The mechanism has reset and is ready to go again.
        d_tryUpdate_3 = service._tryUpdate()
        d_doUpdate_2.callback(sentinel.done2)
        self.assertThat(extract_result(d_tryUpdate_3), Is(sentinel.done2))

    def test_getAllClients(self):
        service = ClusterClientService(Clock())
        uuid1 = factory.make_UUID()
        c1 = DummyConnection()
        service.connections[uuid1] = c1
        uuid2 = factory.make_UUID()
        c2 = DummyConnection()
        service.connections[uuid2] = c2
        clients = service.getAllClients()
        self.assertItemsEqual(clients, {common.Client(c1), common.Client(c2)})

    def test_getAllClients_when_there_are_no_connections(self):
        service = ClusterClientService(Clock())
        service.connections = {}
        self.assertThat(service.getAllClients(), Equals([]))


class TestClusterClientServiceIntervals(MAASTestCase):

    scenarios = (
        (
            "initial",
            {
                "time_running": 0,
                "num_eventloops": None,
                "num_connections": None,
                "expected": ClusterClientService.INTERVAL_LOW,
            },
        ),
        (
            "shortly-after-start",
            {
                "time_running": 10,
                "num_eventloops": 1,  # same as num_connections.
                "num_connections": 1,  # same as num_eventloops.
                "expected": ClusterClientService.INTERVAL_LOW,
            },
        ),
        (
            "no-event-loops",
            {
                "time_running": 1000,
                "num_eventloops": 0,
                "num_connections": sentinel.undefined,
                "expected": ClusterClientService.INTERVAL_LOW,
            },
        ),
        (
            "no-connections",
            {
                "time_running": 1000,
                "num_eventloops": 1,  # anything > 1.
                "num_connections": 0,
                "expected": ClusterClientService.INTERVAL_LOW,
            },
        ),
        (
            "fewer-connections-than-event-loops",
            {
                "time_running": 1000,
                "num_eventloops": 2,  # anything > num_connections.
                "num_connections": 1,  # anything > 0.
                "expected": ClusterClientService.INTERVAL_MID,
            },
        ),
        (
            "default",
            {
                "time_running": 1000,
                "num_eventloops": 3,  # same as num_connections.
                "num_connections": 3,  # same as num_eventloops.
                "expected": ClusterClientService.INTERVAL_HIGH,
            },
        ),
    )

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__calculate_interval(self):
        service = make_inert_client_service()
        service.startService()
        service.clock.advance(self.time_running)
        self.assertEqual(
            self.expected,
            service._calculate_interval(
                self.num_eventloops, self.num_connections
            ),
        )


class TestClusterClient(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestClusterClient, self).setUp()
        self.useFixture(
            ClusterConfigurationFixture(
                maas_url=factory.make_simple_http_url(),
                cluster_uuid=factory.make_UUID(),
            )
        )
        self.maas_id = None

        def set_maas_id(maas_id):
            self.maas_id = maas_id

        self.set_maas_id = self.patch(clusterservice, "set_maas_id")
        self.set_maas_id.side_effect = set_maas_id

        def get_maas_id():
            return self.maas_id

        self.get_maas_id = self.patch(clusterservice, "get_maas_id")
        self.get_maas_id.side_effect = get_maas_id

    def make_running_client(self):
        client = clusterservice.ClusterClient(
            address=("example.com", 1234),
            eventloop="eventloop:pid=12345",
            service=make_inert_client_service(),
        )
        client.service.startService()
        return client

    def patch_authenticate_for_success(self, client):
        authenticate = self.patch_autospec(client, "authenticateRegion")
        authenticate.side_effect = always_succeed_with(True)

    def patch_authenticate_for_failure(self, client):
        authenticate = self.patch_autospec(client, "authenticateRegion")
        authenticate.side_effect = always_succeed_with(False)

    def patch_authenticate_for_error(self, client, exception):
        authenticate = self.patch_autospec(client, "authenticateRegion")
        authenticate.side_effect = always_fail_with(exception)

    def patch_register_for_success(self, client):
        register = self.patch_autospec(client, "registerRackWithRegion")
        register.side_effect = always_succeed_with(True)

    def patch_register_for_failure(self, client):
        register = self.patch_autospec(client, "registerRackWithRegion")
        register.side_effect = always_succeed_with(False)

    def patch_register_for_error(self, client, exception):
        register = self.patch_autospec(client, "registerRackWithRegion")
        register.side_effect = always_fail_with(exception)

    def test_interfaces(self):
        client = self.make_running_client()
        # transport.getHandle() is used by AMP._getPeerCertificate, which we
        # call indirectly via the peerCertificate attribute in IConnection.
        self.patch(client, "transport")
        verifyObject(IConnection, client)

    def test_ident(self):
        client = self.make_running_client()
        client.eventloop = self.getUniqueString()
        self.assertThat(client.ident, Equals(client.eventloop))

    def test_connecting(self):
        client = self.make_running_client()
        client.service.try_connections[client.eventloop] = client
        self.patch_authenticate_for_success(client)
        self.patch_register_for_success(client)
        self.assertEqual(client.service.connections, {})
        wait_for_authenticated = client.authenticated.get()
        self.assertThat(wait_for_authenticated, IsUnfiredDeferred())
        wait_for_ready = client.ready.get()
        self.assertThat(wait_for_ready, IsUnfiredDeferred())
        client.connectionMade()
        # authenticated has been set to True, denoting a successfully
        # authenticated region.
        self.assertTrue(extract_result(wait_for_authenticated))
        # ready has been set with the name of the event-loop.
        self.assertEqual(client.eventloop, extract_result(wait_for_ready))
        self.assertEqual(client.service.try_connections, {})
        self.assertEqual(
            client.service.connections, {client.eventloop: client}
        )

    def test_disconnects_when_there_is_an_existing_connection(self):
        client = self.make_running_client()

        # Pretend that a connection already exists for this address.
        client.service.connections[client.eventloop] = sentinel.connection

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated was set to None to signify that authentication was not
        # attempted.
        self.assertIsNone(extract_result(client.authenticated.get()))
        # ready was set with KeyError to signify that a connection to the
        # same event-loop already existed.
        self.assertRaises(KeyError, extract_result, client.ready.get())

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(
            client.service.connections, {client.eventloop: sentinel.connection}
        )
        self.assertFalse(client.connected)
        self.assertIsNone(client.transport)

    def test_disconnects_when_service_is_not_running(self):
        client = self.make_running_client()
        client.service.running = False

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated was set to None to signify that authentication was not
        # attempted.
        self.assertIsNone(extract_result(client.authenticated.get()))
        # ready was set with RuntimeError to signify that the client
        # service was not running.
        self.assertRaises(RuntimeError, extract_result, client.ready.get())

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(client.service.connections, {})
        self.assertFalse(client.connected)

    def test_disconnects_when_authentication_fails(self):
        client = self.make_running_client()
        self.patch_authenticate_for_failure(client)
        self.patch_register_for_success(client)

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated was set to False.
        self.assertIs(False, extract_result(client.authenticated.get()))
        # ready was set with AuthenticationFailed.
        self.assertRaises(
            exceptions.AuthenticationFailed, extract_result, client.ready.get()
        )

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(client.service.connections, {})
        self.assertFalse(client.connected)

    def test_disconnects_when_authentication_errors(self):
        client = self.make_running_client()
        exception_type = factory.make_exception_type()
        self.patch_authenticate_for_error(client, exception_type())
        self.patch_register_for_success(client)

        logger = self.useFixture(TwistedLoggerFixture())

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated errbacks with the error.
        self.assertRaises(
            exception_type, extract_result, client.authenticated.get()
        )
        # ready also errbacks with the same error.
        self.assertRaises(exception_type, extract_result, client.ready.get())

        # The log was written to.
        self.assertDocTestMatches(
            """...
            Event-loop 'eventloop:pid=12345' handshake failed;
            dropping connection.
            Traceback (most recent call last):...
            """,
            logger.dump(),
        )

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(client.service.connections, {})
        self.assertFalse(client.connected)

    def test_disconnects_when_registration_fails(self):
        client = self.make_running_client()
        self.patch_authenticate_for_success(client)
        self.patch_register_for_failure(client)

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated was set to True because it succeeded.
        self.assertIs(True, extract_result(client.authenticated.get()))
        # ready was set with AuthenticationFailed.
        self.assertRaises(
            exceptions.RegistrationFailed, extract_result, client.ready.get()
        )

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(client.service.connections, {})
        self.assertFalse(client.connected)

    def test_disconnects_when_registration_errors(self):
        client = self.make_running_client()
        exception_type = factory.make_exception_type()
        self.patch_authenticate_for_success(client)
        self.patch_register_for_error(client, exception_type())

        logger = self.useFixture(TwistedLoggerFixture())

        # Connect via an in-memory transport.
        transport = StringTransportWithDisconnection()
        transport.protocol = client
        client.makeConnection(transport)

        # authenticated was set to True because it succeeded.
        self.assertIs(True, extract_result(client.authenticated.get()))
        # ready was set with the exception we made.
        self.assertRaises(exception_type, extract_result, client.ready.get())

        # The log was written to.
        self.assertDocTestMatches(
            """...
            Event-loop 'eventloop:pid=12345' handshake failed;
            dropping connection.
            Traceback (most recent call last):...
            """,
            logger.dump(),
        )

        # The connections list is unchanged because the new connection
        # immediately disconnects.
        self.assertEqual(client.service.connections, {})
        self.assertFalse(client.connected)

    def test_handshakeFailed_does_not_log_when_connection_is_closed(self):
        client = self.make_running_client()
        with TwistedLoggerFixture() as logger:
            client.handshakeFailed(Failure(ConnectionClosed()))
        # ready was set with ConnectionClosed.
        self.assertRaises(ConnectionClosed, extract_result, client.ready.get())
        # Nothing was logged.
        self.assertEqual("", logger.output)

    @inlineCallbacks
    def test_secureConnection_calls_StartTLS_and_Identify(self):
        client = self.make_running_client()

        callRemote = self.patch(client, "callRemote")
        callRemote_return_values = [
            {},  # In response to a StartTLS call.
            {"ident": client.eventloop},  # Identify.
        ]
        callRemote.side_effect = lambda cmd, **kwargs: (
            callRemote_return_values.pop(0)
        )

        transport = self.patch(client, "transport")
        logger = self.useFixture(TwistedLoggerFixture())

        yield client.secureConnection()

        self.assertThat(
            callRemote,
            MockCallsMatch(
                call(amp.StartTLS, **client.get_tls_parameters()),
                call(region.Identify),
            ),
        )

        # The connection is not dropped.
        self.assertThat(transport.loseConnection, MockNotCalled())

        # The certificates used are echoed to the log.
        self.assertDocTestMatches(
            """\
            Host certificate: ...
            ---
            Peer certificate: ...
            """,
            logger.dump(),
        )

    @inlineCallbacks
    def test_secureConnection_disconnects_if_ident_does_not_match(self):
        client = self.make_running_client()

        callRemote = self.patch(client, "callRemote")
        callRemote.side_effect = [
            {},  # In response to a StartTLS call.
            {"ident": "bogus-name"},  # Identify.
        ]

        transport = self.patch(client, "transport")
        logger = self.useFixture(TwistedLoggerFixture())

        yield client.secureConnection()

        # The connection is dropped.
        self.assertThat(transport.loseConnection, MockCalledOnceWith())

        # The log explains why.
        self.assertDocTestMatches(
            """\
            The remote event-loop identifies itself as bogus-name, but
            eventloop:pid=12345 was expected.
            """,
            logger.dump(),
        )

    # XXX: blake_r 2015-02-26 bug=1426089: Failing because of an unknown
    # reason. This is commented out instead of using @skip because of
    # running MAASTwistedRunTest will cause twisted to complain.
    # @inlineCallbacks
    # def test_secureConnection_end_to_end(self):
    #     fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
    #     protocol, connecting = fixture.makeEventLoop()
    #     self.addCleanup((yield connecting))
    #     client = yield getRegionClient()
    #     # XXX: Expose secureConnection() in the client.
    #     yield client._conn.secureConnection()
    #     self.assertTrue(client.isSecure())

    def test_authenticateRegion_accepts_matching_digests(self):
        set_shared_secret_on_filesystem(factory.make_bytes())
        client = self.make_running_client()

        def calculate_digest(_, message):
            # Use the cluster's own authentication responder.
            response = Cluster().authenticate(message)
            return succeed(response)

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = calculate_digest

        d = client.authenticateRegion()
        self.assertTrue(extract_result(d))

    def test_authenticateRegion_rejects_non_matching_digests(self):
        set_shared_secret_on_filesystem(factory.make_bytes())
        client = self.make_running_client()

        def calculate_digest(_, message):
            # Return some nonsense.
            response = {
                "digest": factory.make_bytes(),
                "salt": factory.make_bytes(),
            }
            return succeed(response)

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = calculate_digest

        d = client.authenticateRegion()
        self.assertFalse(extract_result(d))

    def test_authenticateRegion_propagates_errors(self):
        client = self.make_running_client()
        exception_type = factory.make_exception_type()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.return_value = fail(exception_type())

        d = client.authenticateRegion()
        self.assertRaises(exception_type, extract_result, d)

    @inlineCallbacks
    def test_authenticateRegion_end_to_end(self):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop()
        self.addCleanup((yield connecting))
        yield getRegionClient()
        self.assertThat(
            protocol.Authenticate, MockCalledOnceWith(protocol, message=ANY)
        )

    @inlineCallbacks
    def test_registerRackWithRegion_returns_True_when_accepted(self):
        client = self.make_running_client()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with({"system_id": "..."})

        logger = self.useFixture(TwistedLoggerFixture())

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)

        self.assertDocTestMatches(
            "Rack controller '...' registered (via eventloop:pid=12345) with "
            "MAAS version 2.2 or below.",
            logger.output,
        )

    @inlineCallbacks
    def test_registerRackWithRegion_logs_version_if_supplied(self):
        client = self.make_running_client()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with(
            {"system_id": "...", "version": "2.3.0"}
        )

        logger = self.useFixture(TwistedLoggerFixture())

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)

        self.assertDocTestMatches(
            "Rack controller '...' registered (via eventloop:pid=12345) with "
            " MAAS version 2.3.0.",
            logger.output,
        )

    @inlineCallbacks
    def test_registerRackWithRegion_logs_unknown_version_if_empty(self):
        client = self.make_running_client()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with(
            {"system_id": "...", "version": ""}
        )

        logger = self.useFixture(TwistedLoggerFixture())

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)

        self.assertDocTestMatches(
            "Rack controller '...' registered (via eventloop:pid=12345) with "
            " unknown MAAS version.",
            logger.output,
        )

    @inlineCallbacks
    def test_registerRackWithRegion_sets_localIdent(self):
        client = self.make_running_client()

        system_id = factory.make_name("id")
        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with({"system_id": system_id})

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)
        self.assertEqual(system_id, client.localIdent)

    @inlineCallbacks
    def test_registerRackWithRegion_calls_set_maas_id(self):
        client = self.make_running_client()

        system_id = factory.make_name("id")
        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with({"system_id": system_id})

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)
        self.assertThat(self.set_maas_id, MockCalledOnceWith(system_id))

    @inlineCallbacks
    def test_registerRackWithRegion_doesnt_read_maas_id_from_cache(self):
        set_maas_id(factory.make_string())
        os.unlink(get_data_path("/var/lib/maas/maas_id"))

        maas_url = factory.make_simple_http_url()
        hostname = platform.node().split(".")[0]
        interfaces = get_all_interfaces_definition()
        self.useFixture(ClusterConfigurationFixture())
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture(maas_url))
        protocol, connecting = fixture.makeEventLoop()
        self.addCleanup((yield connecting))
        yield getRegionClient()
        self.assertThat(
            protocol.RegisterRackController,
            MockCalledOnceWith(
                protocol,
                system_id="",
                hostname=hostname,
                interfaces=interfaces,
                url=urlparse(maas_url),
                nodegroup_uuid=None,
                beacon_support=True,
                version=get_maas_version(),
            ),
        )
        # Clear cache for the next test
        set_maas_id(None)

    @inlineCallbacks
    def test_registerRackWithRegion_sets_global_labels(self):
        mock_set_global_labels = self.patch(
            clusterservice, "set_global_labels"
        )
        client = self.make_running_client()

        system_id = factory.make_name("id")
        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.side_effect = always_succeed_with(
            {"system_id": system_id, "uuid": "a-b-c"}
        )

        result = yield client.registerRackWithRegion()
        self.assertTrue(result)
        mock_set_global_labels.assert_called_once_with(
            maas_uuid="a-b-c", service_type="rack"
        )

    @inlineCallbacks
    def test_registerRackWithRegion_returns_False_when_rejected(self):
        client = self.make_running_client()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.return_value = fail(
            exceptions.CannotRegisterRackController()
        )

        logger = self.useFixture(TwistedLoggerFixture())

        result = yield client.registerRackWithRegion()
        self.assertFalse(result)

        self.assertDocTestMatches(
            "Rack controller REJECTED by the region "
            "(via eventloop:pid=12345).",
            logger.output,
        )

    @inlineCallbacks
    def test_registerRackWithRegion_propagates_errors(self):
        client = self.make_running_client()
        exception_type = factory.make_exception_type()

        callRemote = self.patch_autospec(client, "callRemote")
        callRemote.return_value = fail(exception_type())

        caught_exc = None
        try:
            yield client.registerRackWithRegion()
        except Exception as exc:
            caught_exc = exc
        self.assertIsInstance(caught_exc, exception_type)

    @inlineCallbacks
    def test_registerRackWithRegion_end_to_end(self):
        maas_url = factory.make_simple_http_url()
        hostname = "rackcontrol.example.com"
        self.patch_autospec(
            clusterservice, "gethostname"
        ).return_value = hostname
        interfaces = get_all_interfaces_definition()
        self.useFixture(ClusterConfigurationFixture())
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture(maas_url))
        protocol, connecting = fixture.makeEventLoop()
        self.addCleanup((yield connecting))
        yield getRegionClient()
        self.assertThat(
            protocol.RegisterRackController,
            MockCalledOnceWith(
                protocol,
                system_id="",
                hostname=hostname,
                interfaces=interfaces,
                url=urlparse(maas_url),
                nodegroup_uuid=None,
                beacon_support=True,
                version=get_maas_version(),
            ),
        )


class TestClusterClientCheckerService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def make_client(self):
        client = Mock()
        client.return_value = succeed(None)
        return client

    def test_init_sets_up_timer_correctly(self):
        service = ClusterClientCheckerService(
            sentinel.client_service, sentinel.clock
        )
        self.assertThat(
            service,
            MatchesStructure.byEquality(
                call=(service.tryLoop, (), {}),
                step=30,
                client_service=sentinel.client_service,
                clock=sentinel.clock,
            ),
        )

    def test_tryLoop_calls_loop(self):
        service = ClusterClientCheckerService(
            sentinel.client_service, sentinel.clock
        )
        mock_loop = self.patch(service, "loop")
        mock_loop.return_value = succeed(None)
        service.tryLoop()
        self.assertThat(mock_loop, MockCalledOnceWith())

    def test_loop_does_nothing_with_no_clients(self):
        mock_client_service = MagicMock()
        mock_client_service.getAllClients.return_value = []
        service = ClusterClientCheckerService(mock_client_service, reactor)
        # Test will timeout if this blocks longer than 5 seconds.
        return service.loop()

    @inlineCallbacks
    def test_loop_calls_ping_for_each_client(self):
        clients = [self.make_client() for _ in range(3)]
        mock_client_service = MagicMock()
        mock_client_service.getAllClients.return_value = clients
        service = ClusterClientCheckerService(mock_client_service, reactor)
        yield service.loop()
        for client in clients:
            self.expectThat(
                client, MockCalledOnceWith(common.Ping, _timeout=10)
            )

    @inlineCallbacks
    def test_ping_calls_loseConnection_on_failure(self):
        client = MagicMock()
        client.return_value = fail(factory.make_exception())
        mock_client_service = MagicMock()
        service = ClusterClientCheckerService(mock_client_service, reactor)
        yield service._ping(client)
        self.assertThat(
            client._conn.transport.loseConnection, MockCalledOnceWith()
        )


class TestClusterProtocol_ListSupportedArchitectures(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.ListSupportedArchitectures.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_returns_architectures(self):
        architectures = yield call_responder(
            Cluster(), cluster.ListSupportedArchitectures, {}
        )
        # Assert that one of the built-in architectures is in the data
        # returned by ListSupportedArchitectures.
        self.assertIn(
            {"name": "i386/generic", "description": "i386"},
            architectures["architectures"],
        )


class TestClusterProtocol_ListOperatingSystems(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.ListOperatingSystems.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_returns_oses(self):
        # Patch in some operating systems with some randomised data. See
        # StubOS for details of the rules that are used to populate the
        # non-random elements.
        operating_systems = [
            StubOS(
                factory.make_name("os"),
                releases=[
                    (factory.make_name("name"), factory.make_name("title"))
                    for _ in range(randint(2, 5))
                ],
            )
            for _ in range(randint(2, 5))
        ]
        self.patch(
            osystems_rpc_module,
            "OperatingSystemRegistry",
            [(os.name, os) for os in operating_systems],
        )
        osystems = yield call_responder(
            Cluster(), cluster.ListOperatingSystems, {}
        )
        # The fully-populated output from gen_operating_systems() sent
        # back over the wire.
        expected_osystems = list(gen_operating_systems())
        for expected_osystem in expected_osystems:
            expected_osystem["releases"] = list(expected_osystem["releases"])
        expected = {"osystems": expected_osystems}
        self.assertEqual(expected, osystems)


class TestClusterProtocol_GetOSReleaseTitle(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.GetOSReleaseTitle.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_calls_get_os_release_title(self):
        title = factory.make_name("title")
        get_os_release_title = self.patch(
            clusterservice, "get_os_release_title"
        )
        get_os_release_title.return_value = title
        arguments = {
            "osystem": factory.make_name("osystem"),
            "release": factory.make_name("release"),
        }
        observed = yield call_responder(
            Cluster(), cluster.GetOSReleaseTitle, arguments
        )
        expected = {"title": title}
        self.assertEqual(expected, observed)
        # The arguments are passed to the responder positionally.
        self.assertThat(
            get_os_release_title,
            MockCalledOnceWith(arguments["osystem"], arguments["release"]),
        )

    @inlineCallbacks
    def test_exception_when_os_does_not_exist(self):
        # A remote NoSuchOperatingSystem exception is re-raised locally.
        get_os_release_title = self.patch(
            clusterservice, "get_os_release_title"
        )
        get_os_release_title.side_effect = exceptions.NoSuchOperatingSystem()
        arguments = {
            "osystem": factory.make_name("osystem"),
            "release": factory.make_name("release"),
        }
        with ExpectedException(exceptions.NoSuchOperatingSystem):
            yield call_responder(
                Cluster(), cluster.GetOSReleaseTitle, arguments
            )


class TestClusterProtocol_ValidateLicenseKey(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.ValidateLicenseKey.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_calls_validate_license_key(self):
        validate_license_key = self.patch(
            clusterservice, "validate_license_key"
        )
        validate_license_key.return_value = factory.pick_bool()
        arguments = {
            "osystem": factory.make_name("osystem"),
            "release": factory.make_name("release"),
            "key": factory.make_name("key"),
        }
        observed = yield call_responder(
            Cluster(), cluster.ValidateLicenseKey, arguments
        )
        expected = {"is_valid": validate_license_key.return_value}
        self.assertEqual(expected, observed)
        # The arguments are passed to the responder positionally.
        self.assertThat(
            validate_license_key,
            MockCalledOnceWith(
                arguments["osystem"], arguments["release"], arguments["key"]
            ),
        )

    @inlineCallbacks
    def test_exception_when_os_does_not_exist(self):
        # A remote NoSuchOperatingSystem exception is re-raised locally.
        validate_license_key = self.patch(
            clusterservice, "validate_license_key"
        )
        validate_license_key.side_effect = exceptions.NoSuchOperatingSystem()
        arguments = {
            "osystem": factory.make_name("osystem"),
            "release": factory.make_name("release"),
            "key": factory.make_name("key"),
        }
        with ExpectedException(exceptions.NoSuchOperatingSystem):
            yield call_responder(
                Cluster(), cluster.ValidateLicenseKey, arguments
            )


class TestClusterProtocol_GetPreseedData(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def make_arguments(self):
        return {
            "osystem": factory.make_name("osystem"),
            "preseed_type": factory.make_name("preseed_type"),
            "node_system_id": factory.make_name("system_id"),
            "node_hostname": factory.make_name("hostname"),
            "consumer_key": factory.make_name("consumer_key"),
            "token_key": factory.make_name("token_key"),
            "token_secret": factory.make_name("token_secret"),
            "metadata_url": urlparse(
                "https://%s/path/to/metadata" % factory.make_hostname()
            ),
        }

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.GetPreseedData.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_calls_get_preseed_data(self):
        get_preseed_data = self.patch(clusterservice, "get_preseed_data")
        get_preseed_data.return_value = factory.make_name("data")
        arguments = self.make_arguments()
        observed = yield call_responder(
            Cluster(), cluster.GetPreseedData, arguments
        )
        expected = {"data": get_preseed_data.return_value}
        self.assertEqual(expected, observed)
        # The arguments are passed to the responder positionally.
        self.assertThat(
            get_preseed_data,
            MockCalledOnceWith(
                arguments["osystem"],
                arguments["preseed_type"],
                arguments["node_system_id"],
                arguments["node_hostname"],
                arguments["consumer_key"],
                arguments["token_key"],
                arguments["token_secret"],
                arguments["metadata_url"],
            ),
        )

    @inlineCallbacks
    def test_exception_when_os_does_not_exist(self):
        # A remote NoSuchOperatingSystem exception is re-raised locally.
        get_preseed_data = self.patch(clusterservice, "get_preseed_data")
        get_preseed_data.side_effect = exceptions.NoSuchOperatingSystem()
        arguments = self.make_arguments()
        with ExpectedException(exceptions.NoSuchOperatingSystem):
            yield call_responder(Cluster(), cluster.GetPreseedData, arguments)

    @inlineCallbacks
    def test_exception_when_preseed_not_implemented(self):
        # A remote NotImplementedError exception is re-raised locally.
        # Choose an operating system which has not overridden the
        # default compose_preseed.
        osystem_name = next(
            osystem_name
            for osystem_name, osystem in OperatingSystemRegistry
            if osystem.compose_preseed == OperatingSystem.compose_preseed
        )
        arguments = self.make_arguments()
        arguments["osystem"] = osystem_name
        with ExpectedException(exceptions.NoSuchOperatingSystem):
            yield call_responder(Cluster(), cluster.GetPreseedData, arguments)


class TestClusterProtocol_PowerOn_PowerOff_PowerCycle(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    scenarios = (
        (
            "power-on",
            {"command": cluster.PowerOn, "expected_power_change": "on"},
        ),
        (
            "power-off",
            {"command": cluster.PowerOff, "expected_power_change": "off"},
        ),
        (
            "power-cycle",
            {"command": cluster.PowerCycle, "expected_power_change": "cycle"},
        ),
    )

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(self.command.commandName)
        self.assertIsNotNone(responder)

    def test_executes_maybe_change_power_state(self):
        maybe_change_power_state = self.patch(
            clusterservice, "maybe_change_power_state"
        )

        system_id = factory.make_name("system_id")
        hostname = factory.make_name("hostname")
        power_type = factory.make_name("power_type")
        context = {factory.make_name("name"): factory.make_name("value")}

        d = call_responder(
            Cluster(),
            self.command,
            {
                "system_id": system_id,
                "hostname": hostname,
                "power_type": power_type,
                "context": context,
            },
        )

        def check(response):
            self.assertThat(
                maybe_change_power_state,
                MockCalledOnceWith(
                    system_id,
                    hostname,
                    power_type,
                    power_change=self.expected_power_change,
                    context=context,
                ),
            )

        return d.addCallback(check)

    def test_power_on_can_propagate_UnknownPowerType(self):
        self.patch(
            clusterservice, "maybe_change_power_state"
        ).side_effect = exceptions.UnknownPowerType

        d = call_responder(
            Cluster(),
            self.command,
            {
                "system_id": "id",
                "hostname": "hostname",
                "power_type": "type",
                "context": {},
            },
        )
        # If the call doesn't fail then we have a test failure; we're
        # *expecting* UnknownPowerType to be raised.
        d.addCallback(self.fail)

        def check(failure):
            failure.trap(exceptions.UnknownPowerType)

        return d.addErrback(check)

    def test_power_on_can_propagate_NotImplementedError(self):
        self.patch(
            clusterservice, "maybe_change_power_state"
        ).side_effect = NotImplementedError

        d = call_responder(
            Cluster(),
            self.command,
            {
                "system_id": "id",
                "hostname": "hostname",
                "power_type": "type",
                "context": {},
            },
        )
        # If the call doesn't fail then we have a test failure; we're
        # *expecting* NotImplementedError to be raised.
        d.addCallback(self.fail)

        def check(failure):
            failure.trap(NotImplementedError)

        return d.addErrback(check)

    def test_power_on_can_propagate_PowerActionFail(self):
        self.patch(
            clusterservice, "maybe_change_power_state"
        ).side_effect = exceptions.PowerActionFail

        d = call_responder(
            Cluster(),
            self.command,
            {
                "system_id": "id",
                "hostname": "hostname",
                "power_type": "type",
                "context": {},
            },
        )
        # If the call doesn't fail then we have a test failure; we're
        # *expecting* PowerActionFail to be raised.
        d.addCallback(self.fail)

        def check(failure):
            failure.trap(exceptions.PowerActionFail)

        return d.addErrback(check)

    def test_power_on_can_propagate_PowerActionAlreadyInProgress(self):
        self.patch(
            clusterservice, "maybe_change_power_state"
        ).side_effect = exceptions.PowerActionAlreadyInProgress

        d = call_responder(
            Cluster(),
            self.command,
            {
                "system_id": "id",
                "hostname": "hostname",
                "power_type": "type",
                "context": {},
            },
        )
        # If the call doesn't fail then we have a test failure; we're
        # *expecting* PowerActionFail to be raised.
        d.addCallback(self.fail)

        def check(failure):
            failure.trap(exceptions.PowerActionAlreadyInProgress)

        return d.addErrback(check)


class TestClusterProtocol_PowerQuery(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.PowerQuery.commandName)
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_returns_power_state(self):
        state = random.choice(["on", "off"])
        perform_power_driver_query = self.patch(
            power_module, "perform_power_driver_query"
        )
        perform_power_driver_query.return_value = state
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        arguments = {
            "system_id": factory.make_name("system"),
            "hostname": factory.make_name("hostname"),
            "power_type": power_driver.name,
            "context": factory.make_name("context"),
        }

        # Make sure power driver doesn't check for installed packages.
        self.patch_autospec(
            power_driver, "detect_missing_packages"
        ).return_value = []

        observed = yield call_responder(
            Cluster(), cluster.PowerQuery, arguments
        )
        self.assertEqual({"state": state, "error_msg": None}, observed)
        self.assertThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                arguments["system_id"],
                arguments["hostname"],
                arguments["power_type"],
                arguments["context"],
            ),
        )

    @inlineCallbacks
    def test_returns_power_error(self):
        perform_power_driver_query = self.patch(
            power_module, "perform_power_driver_query"
        )
        perform_power_driver_query.side_effect = PowerError("Error message")
        power_driver = random.choice(
            [driver for _, driver in PowerDriverRegistry if driver.queryable]
        )
        arguments = {
            "system_id": factory.make_name("system"),
            "hostname": factory.make_name("hostname"),
            "power_type": power_driver.name,
            "context": factory.make_name("context"),
        }

        # Make sure power driver doesn't check for installed packages.
        self.patch_autospec(
            power_driver, "detect_missing_packages"
        ).return_value = []

        observed = yield call_responder(
            Cluster(), cluster.PowerQuery, arguments
        )
        self.assertEqual(
            {"state": "error", "error_msg": "Error message"}, observed
        )
        self.assertThat(
            perform_power_driver_query,
            MockCalledOnceWith(
                arguments["system_id"],
                arguments["hostname"],
                arguments["power_type"],
                arguments["context"],
            ),
        )


class TestClusterProtocol_ConfigureDHCP(MAASTestCase):

    scenarios = (
        (
            "DHCPv4",
            {
                "dhcp_server": (dhcp, "DHCPv4Server"),
                "command": cluster.ConfigureDHCPv4,
                "make_network": factory.make_ipv4_network,
                "make_shared_network": make_shared_network_v1,
                "make_shared_network_kwargs": {},
                "concurrency_lock": concurrency.dhcpv4,
            },
        ),
        (
            "DHCPv4,V2",
            {
                "dhcp_server": (dhcp, "DHCPv4Server"),
                "command": cluster.ConfigureDHCPv4_V2,
                "make_network": factory.make_ipv4_network,
                "make_shared_network": make_shared_network,
                "make_shared_network_kwargs": {"with_interface": True},
                "concurrency_lock": concurrency.dhcpv4,
            },
        ),
        (
            "DHCPv6",
            {
                "dhcp_server": (dhcp, "DHCPv6Server"),
                "command": cluster.ConfigureDHCPv6,
                "make_network": factory.make_ipv6_network,
                "make_shared_network": make_shared_network_v1,
                "make_shared_network_kwargs": {},
                "concurrency_lock": concurrency.dhcpv6,
            },
        ),
        (
            "DHCPv6,V2",
            {
                "dhcp_server": (dhcp, "DHCPv6Server"),
                "command": cluster.ConfigureDHCPv6_V2,
                "make_network": factory.make_ipv6_network,
                "make_shared_network": make_shared_network,
                "make_shared_network_kwargs": {"with_interface": True},
                "concurrency_lock": concurrency.dhcpv6,
            },
        ),
    )

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        self.assertIsNotNone(
            Cluster().locateResponder(self.command.commandName)
        )

    @inlineCallbacks
    def test__executes_configure_dhcp(self):
        DHCPServer = self.patch_autospec(*self.dhcp_server)
        configure = self.patch_autospec(dhcp, "configure")

        omapi_key = factory.make_name("key")
        failover_peers = [make_failover_peer_config()]
        shared_networks = [
            self.make_shared_network(**self.make_shared_network_kwargs)
        ]
        shared_networks = fix_shared_networks_failover(
            shared_networks, failover_peers
        )
        hosts = [make_host()]
        interfaces = [make_interface()]

        yield call_responder(
            Cluster(),
            self.command,
            {
                "omapi_key": omapi_key,
                "failover_peers": failover_peers,
                "shared_networks": shared_networks,
                "hosts": hosts,
                "interfaces": interfaces,
            },
        )

        # The `shared_networks` structure is always the V2 style.
        dhcp.upgrade_shared_networks(shared_networks)

        self.assertThat(DHCPServer, MockCalledOnceWith(omapi_key))
        self.assertThat(
            configure,
            MockCalledOnceWith(
                DHCPServer.return_value,
                failover_peers,
                shared_networks,
                hosts,
                interfaces,
                None,
            ),
        )

    @inlineCallbacks
    def test__limits_concurrency(self):
        self.patch_autospec(*self.dhcp_server)

        def check_dhcp_locked(
            server,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ):
            self.assertTrue(self.concurrency_lock.locked)
            # While we're here, check this is the IO thread.
            self.expectThat(isInIOThread(), Is(True))

        self.patch(dhcp, "configure", check_dhcp_locked)

        self.assertFalse(self.concurrency_lock.locked)
        yield call_responder(
            Cluster(),
            self.command,
            {
                "omapi_key": factory.make_name("key"),
                "failover_peers": [],
                "shared_networks": [],
                "hosts": [],
                "interfaces": [],
            },
        )
        self.assertFalse(self.concurrency_lock.locked)

    @inlineCallbacks
    def test__propagates_CannotConfigureDHCP(self):
        configure = self.patch_autospec(dhcp, "configure")
        configure.side_effect = exceptions.CannotConfigureDHCP(
            "Deliberate failure"
        )
        omapi_key = factory.make_name("key")
        failover_peers = [make_failover_peer_config()]
        shared_networks = [self.make_shared_network()]
        shared_networks = fix_shared_networks_failover(
            shared_networks, failover_peers
        )
        hosts = [make_host()]
        interfaces = [make_interface()]

        with ExpectedException(exceptions.CannotConfigureDHCP):
            yield call_responder(
                Cluster(),
                self.command,
                {
                    "omapi_key": omapi_key,
                    "failover_peers": failover_peers,
                    "shared_networks": shared_networks,
                    "hosts": hosts,
                    "interfaces": interfaces,
                },
            )

    @inlineCallbacks
    def test__times_out(self):
        self.patch_autospec(*self.dhcp_server)
        self.patch(clusterservice, "DHCP_TIMEOUT", 1)

        def check_dhcp_locked(
            server,
            failover_peers,
            shared_networks,
            hosts,
            interfaces,
            global_dhcp_snippets,
        ):
            # Pause longer than the timeout.
            return pause(5)

        self.patch(dhcp, "configure", check_dhcp_locked)

        with ExpectedException(exceptions.CannotConfigureDHCP):
            yield call_responder(
                Cluster(),
                self.command,
                {
                    "omapi_key": factory.make_name("key"),
                    "failover_peers": [],
                    "shared_networks": [],
                    "hosts": [],
                    "interfaces": [],
                },
            )


class TestClusterProtocol_ValidateDHCP(MAASTestCase):

    scenarios = (
        (
            "DHCPv4",
            {
                "dhcp_server": (dhcp, "DHCPv4Server"),
                "command": cluster.ValidateDHCPv4Config,
                "make_network": factory.make_ipv4_network,
                "make_shared_network": make_shared_network_v1,
            },
        ),
        (
            "DHCPv4,V2",
            {
                "dhcp_server": (dhcp, "DHCPv4Server"),
                "command": cluster.ValidateDHCPv4Config_V2,
                "make_network": factory.make_ipv4_network,
                "make_shared_network": make_shared_network,
            },
        ),
        (
            "DHCPv6",
            {
                "dhcp_server": (dhcp, "DHCPv6Server"),
                "command": cluster.ValidateDHCPv6Config,
                "make_network": factory.make_ipv6_network,
                "make_shared_network": make_shared_network_v1,
            },
        ),
        (
            "DHCPv6,V2",
            {
                "dhcp_server": (dhcp, "DHCPv6Server"),
                "command": cluster.ValidateDHCPv6Config_V2,
                "make_network": factory.make_ipv6_network,
                "make_shared_network": make_shared_network,
            },
        ),
    )

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestClusterProtocol_ValidateDHCP, self).setUp()
        # Temporarily prevent hostname resolution when generating DHCP
        # configuration. This is tested elsewhere.
        self.useFixture(DHCPConfigNameResolutionDisabled())

    def test__is_registered(self):
        self.assertIsNotNone(
            Cluster().locateResponder(self.command.commandName)
        )

    @inlineCallbacks
    def test__validates_good_dhcp_config(self):
        self.patch(dhcp, "call_and_check").return_value = None

        omapi_key = factory.make_name("key")
        failover_peers = [make_failover_peer_config()]
        shared_networks = [self.make_shared_network()]
        shared_networks = fix_shared_networks_failover(
            shared_networks, failover_peers
        )
        hosts = [make_host()]
        interfaces = [make_interface()]

        response = yield call_responder(
            Cluster(),
            self.command,
            {
                "omapi_key": omapi_key,
                "failover_peers": failover_peers,
                "shared_networks": shared_networks,
                "hosts": hosts,
                "interfaces": interfaces,
            },
        )
        self.assertEquals(None, response["errors"])

    @inlineCallbacks
    def test__validates_bad_dhcp_config(self):
        dhcpd_error = (
            "Internet Systems Consortium DHCP Server 4.3.3\n"
            "Copyright 2004-2015 Internet Systems Consortium.\n"
            "All rights reserved.\n"
            "For info, please visit https://www.isc.org/software/dhcp/\n"
            "/tmp/maas-dhcpd-z5c7hfzt line 14: semicolon expected.\n"
            "ignore \n"
            "^\n"
            "Configuration file errors encountered -- exiting\n"
            "\n"
            "If you think you have received this message due to a bug rather\n"
            "than a configuration issue please read the section on submitting"
            "\n"
            "bugs on either our web page at www.isc.org or in the README file"
            "\n"
            "before submitting a bug.  These pages explain the proper\n"
            "process and the information we find helpful for debugging..\n"
            "\n"
            "exiting."
        )
        self.patch(dhcp, "call_and_check").side_effect = ExternalProcessError(
            returncode=1, cmd=("dhcpd",), output=dhcpd_error
        )

        omapi_key = factory.make_name("key")
        failover_peers = [make_failover_peer_config()]
        shared_networks = [self.make_shared_network()]
        shared_networks = fix_shared_networks_failover(
            shared_networks, failover_peers
        )
        hosts = [make_host()]
        interfaces = [make_interface()]

        response = yield call_responder(
            Cluster(),
            self.command,
            {
                "omapi_key": omapi_key,
                "failover_peers": failover_peers,
                "shared_networks": shared_networks,
                "hosts": hosts,
                "interfaces": interfaces,
            },
        )
        self.assertEqual(
            [
                {
                    "error": "semicolon expected.",
                    "line_num": 14,
                    "line": "ignore ",
                    "position": "^",
                }
            ],
            response["errors"],
        )


class TestClusterProtocol_EvaluateTag(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.EvaluateTag.commandName)
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_happy_path(self):
        self.useFixture(ClusterConfigurationFixture())
        # Prevent real work being done, which would involve HTTP calls.
        self.patch_autospec(tags, "process_node_tags")
        rack_id = factory.make_name("rack")

        nodes = [{"system_id": factory.make_name("node")} for _ in range(3)]

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        response = yield call_responder(
            conn_cluster,
            cluster.EvaluateTag,
            {
                "system_id": rack_id,
                "tag_name": "all-nodes",
                "tag_definition": "//*",
                "tag_nsmap": [
                    {"prefix": "foo", "uri": "http://foo.example.com/"}
                ],
                "credentials": "abc:def:ghi",
                "nodes": nodes,
            },
        )

        self.assertEqual({}, response)

    @inlineCallbacks
    def test__calls_through_to_evaluate_tag_helper(self):
        evaluate_tag = self.patch_autospec(clusterservice, "evaluate_tag")

        tag_name = factory.make_name("tag-name")
        tag_definition = factory.make_name("tag-definition")
        tag_ns_prefix = factory.make_name("tag-ns-prefix")
        tag_ns_uri = factory.make_name("tag-ns-uri")

        consumer_key = factory.make_name("ckey")
        resource_token = factory.make_name("rtok")
        resource_secret = factory.make_name("rsec")
        credentials = convert_tuple_to_string(
            (consumer_key, resource_token, resource_secret)
        )
        rack_id = factory.make_name("rack")
        nodes = [{"system_id": factory.make_name("node")} for _ in range(3)]

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        yield call_responder(
            conn_cluster,
            cluster.EvaluateTag,
            {
                "system_id": rack_id,
                "tag_name": tag_name,
                "tag_definition": tag_definition,
                "tag_nsmap": [{"prefix": tag_ns_prefix, "uri": tag_ns_uri}],
                "credentials": credentials,
                "nodes": nodes,
            },
        )

        self.assertThat(
            evaluate_tag,
            MockCalledOnceWith(
                rack_id,
                nodes,
                tag_name,
                tag_definition,
                {tag_ns_prefix: tag_ns_uri},
                (consumer_key, resource_token, resource_secret),
                conn_cluster.service.maas_url,
            ),
        )


class MAASTestCaseThatWaitsForDeferredThreads(MAASTestCase):
    """Capture deferred threads and wait for them during teardown.

    This will capture calls to `deferToThread` in the `clusterservice` module,
    and can be useful when work is deferred to threads in a way that cannot be
    observed via the system under test.

    Use of this may be an indicator for code that is poorly designed for
    testing. Consider refactoring so that your tests can explicitly deal with
    threads that have been deferred outside of the reactor.
    """

    # Subclasses can override this, but they MUST choose a runner that runs
    # the test itself and all clean-up functions in the Twisted reactor.
    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super().setUp()
        self.__deferToThreadOrig = clusterservice.deferToThread
        self.patch(clusterservice, "deferToThread", self.__deferToThread)

    def __deferToThread(self, f, *args, **kwargs):
        d = self.__deferToThreadOrig(f, *args, **kwargs)
        self.addCleanup(lambda: d)  # Wait during teardown.
        return d


class TestClusterProtocol_Refresh(MAASTestCaseThatWaitsForDeferredThreads):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.RefreshRackControllerInfo.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test__raises_refresh_already_in_progress_when_locked(self):
        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")

        with NamedLock("refresh"):
            with ExpectedException(exceptions.RefreshAlreadyInProgress):
                yield call_responder(
                    Cluster(),
                    cluster.RefreshRackControllerInfo,
                    {
                        "system_id": system_id,
                        "consumer_key": consumer_key,
                        "token_key": token_key,
                        "token_secret": token_secret,
                    },
                )

    @inlineCallbacks
    def test__acquires_lock_when_refreshing_releases_when_done(self):
        def mock_refresh(*args, **kwargs):
            lock = NamedLock("refresh")
            self.assertTrue(lock.is_locked())

        self.patch(clusterservice, "refresh", mock_refresh)
        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")

        yield call_responder(
            Cluster(),
            cluster.RefreshRackControllerInfo,
            {
                "system_id": system_id,
                "consumer_key": consumer_key,
                "token_key": token_key,
                "token_secret": token_secret,
            },
        )

        lock = NamedLock("refresh")
        self.assertFalse(lock.is_locked())

    @inlineCallbacks
    def test__releases_on_error(self):
        exception = factory.make_exception()
        self.patch(clusterservice, "refresh").side_effect = exception
        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        with TwistedLoggerFixture() as logger:
            yield call_responder(
                conn_cluster,
                cluster.RefreshRackControllerInfo,
                {
                    "system_id": system_id,
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
            )

        # The failure is logged
        self.assertDocTestMatches(
            """
            Failed to refresh the rack controller.
            Traceback (most recent call last):
            ...
            maastesting.factory.TestException#...:
            """,
            logger.output,
        )

        # The lock is released
        lock = NamedLock("refresh")
        self.assertFalse(lock.is_locked())

    @inlineCallbacks
    def test__defers_refresh_to_thread(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.side_effect = [
            succeed(None),
            succeed(
                {
                    "hostname": "",
                    "architecture": "",
                    "osystem": "",
                    "distro_series": "",
                    "interfaces": {},
                }
            ),
        ]

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")

        conn_cluster = Cluster()
        conn_cluster.service = MagicMock()
        conn_cluster.service.maas_url = factory.make_simple_http_url()

        yield call_responder(
            conn_cluster,
            cluster.RefreshRackControllerInfo,
            {
                "system_id": system_id,
                "consumer_key": consumer_key,
                "token_key": token_key,
                "token_secret": token_secret,
            },
        )

        self.assertThat(
            mock_deferToThread,
            MockAnyCall(
                clusterservice.refresh,
                system_id,
                consumer_key,
                token_key,
                token_secret,
                ANY,
            ),
        )

    @inlineCallbacks
    def test_returns_extra_info(self):
        self.patch_autospec(clusterservice, "refresh")

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        hostname = factory.make_hostname()
        architecture = factory.make_name("architecture")
        osystem = factory.make_name("osystem")
        distro_series = factory.make_name("distro_series")
        maas_version = factory.make_name("maas_version")
        self.patch_autospec(clusterservice, "get_sys_info").return_value = {
            "hostname": hostname,
            "osystem": osystem,
            "distro_series": distro_series,
            "architecture": architecture,
            "interfaces": {},
            "maas_version": maas_version,
        }

        response = yield call_responder(
            Cluster(),
            cluster.RefreshRackControllerInfo,
            {
                "system_id": system_id,
                "consumer_key": consumer_key,
                "token_key": token_key,
                "token_secret": token_secret,
            },
        )

        self.assertEqual(
            {
                "hostname": hostname,
                "osystem": osystem,
                "distro_series": distro_series,
                "architecture": architecture,
                "maas_version": maas_version,
                "interfaces": {},
            },
            response,
        )


class TestClusterProtocol_ScanNetworks(
    MAASTestCaseThatWaitsForDeferredThreads
):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.ScanNetworks.commandName)
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test__raises_refresh_already_in_progress_when_locked(self):
        with NamedLock("scan-networks"):
            with ExpectedException(exceptions.ScanNetworksAlreadyInProgress):
                yield call_responder(
                    Cluster(), cluster.ScanNetworks, {"scan_all": True}
                )

    @inlineCallbacks
    def test__acquires_lock_when_scanning_releases_when_done(self):
        def mock_scan(*args, **kwargs):
            lock = NamedLock("scan-networks")
            self.assertTrue(lock.is_locked())

        self.patch(clusterservice, "executeScanNetworksSubprocess", mock_scan)

        yield call_responder(
            Cluster(), cluster.ScanNetworks, {"scan_all": True}
        )

        lock = NamedLock("scan-networks")
        self.assertFalse(lock.is_locked())

    @inlineCallbacks
    def test__releases_on_error(self):
        exception = factory.make_exception()
        mock_scan = self.patch(clusterservice, "executeScanNetworksSubprocess")
        mock_scan.side_effect = exception

        with TwistedLoggerFixture() as logger:
            yield call_responder(
                Cluster(), cluster.ScanNetworks, {"scan_all": True}
            )

        # The failure is logged
        self.assertDocTestMatches(
            """
            Failed to scan all networks.
            Traceback (most recent call last):
            ...
            maastesting.factory.TestException#...:
            """,
            logger.output,
        )

        # The lock is released
        lock = NamedLock("scan-networks")
        self.assertFalse(lock.is_locked())

    @inlineCallbacks
    def test__wraps_subprocess_scan_in_maybeDeferred(self):
        mock_maybeDeferred = self.patch_autospec(
            clusterservice, "maybeDeferred"
        )
        mock_maybeDeferred.side_effect = (succeed(None),)

        yield call_responder(
            Cluster(), cluster.ScanNetworks, {"scan_all": True}
        )

        self.assertThat(
            mock_maybeDeferred,
            MockCalledOnceWith(
                clusterservice.executeScanNetworksSubprocess,
                cidrs=None,
                force_ping=None,
                interface=None,
                scan_all=True,
                slow=None,
                threads=None,
            ),
        )

    def test_get_scan_all_networks_args_asserts_for_invalid_config(self):
        with ExpectedException(AssertionError, "Invalid scan parameters.*"):
            get_scan_all_networks_args()

    def test_get_scan_all_networks_args_returns_expected_binary_args(self):
        args = get_scan_all_networks_args(scan_all=True)
        self.assertThat(
            args,
            Equals(
                [get_maas_common_command().encode("utf-8"), b"scan-network"]
            ),
        )

    def test_get_scan_all_networks_args_sudo(self):
        is_dev_environment_mock = self.patch_autospec(
            clusterservice, "is_dev_environment"
        )
        is_dev_environment_mock.return_value = False
        args = get_scan_all_networks_args(scan_all=True)
        self.assertThat(
            args,
            Equals(
                [
                    b"sudo",
                    b"-n",
                    get_maas_common_command().encode("utf-8"),
                    b"scan-network",
                ]
            ),
        )

    def test_get_scan_all_networks_args_returns_supplied_cidrs(self):
        args = get_scan_all_networks_args(
            cidrs=[IPNetwork("192.168.0.0/24"), IPNetwork("192.168.1.0/24")]
        )
        self.assertThat(
            args,
            Equals(
                [
                    get_maas_common_command().encode("utf-8"),
                    b"scan-network",
                    b"192.168.0.0/24",
                    b"192.168.1.0/24",
                ]
            ),
        )

    def test_get_scan_all_networks_args_returns_supplied_interface(self):
        args = get_scan_all_networks_args(interface="eth0")
        self.assertThat(
            args,
            Equals(
                [
                    get_maas_common_command().encode("utf-8"),
                    b"scan-network",
                    b"eth0",
                ]
            ),
        )

    def test_get_scan_all_networks_with_all_optional_arguments(self):
        threads = random.randint(1, 10)
        args = get_scan_all_networks_args(
            scan_all=False,
            slow=True,
            threads=threads,
            force_ping=True,
            interface="eth0",
            cidrs=[IPNetwork("192.168.0.0/24"), IPNetwork("192.168.1.0/24")],
        )
        self.assertThat(
            args,
            Equals(
                [
                    get_maas_common_command().encode("utf-8"),
                    b"scan-network",
                    b"--threads",
                    str(threads).encode("utf-8"),
                    b"--ping",
                    b"--slow",
                    b"eth0",
                    b"192.168.0.0/24",
                    b"192.168.1.0/24",
                ]
            ),
        )

    @inlineCallbacks
    def test_spawnProcessAndNullifyStdout_nullifies_stdout(self):
        done, protocol = makeDeferredWithProcessProtocol()
        args = [b"/bin/bash", b"-c", b"echo foo"]
        outReceived = Mock()
        protocol.outReceived = outReceived
        spawnProcessAndNullifyStdout(protocol, args)
        yield done
        self.assertThat(outReceived, MockNotCalled())

    @inlineCallbacks
    def test_spawnProcessAndNullifyStdout_captures_stderr(self):
        done, protocol = makeDeferredWithProcessProtocol()
        args = [b"/bin/bash", b"-c", b"echo foo >&2"]
        errReceived = Mock()
        protocol.errReceived = errReceived
        spawnProcessAndNullifyStdout(protocol, args)
        yield done
        self.assertThat(errReceived, MockCalledOnceWith(b"foo\n"))

    @inlineCallbacks
    def test_executeScanNetworksSubprocess(self):
        mock_scan_args = self.patch(
            clusterservice, "get_scan_all_networks_args"
        )
        mock_scan_args.return_value = [b"/bin/bash", b"-c", b"echo -n foo >&2"]
        mock_log_msg = self.patch(clusterservice.log, "msg")
        d = executeScanNetworksSubprocess()
        yield d
        self.assertThat(
            mock_log_msg, MockCalledOnceWith("Scan all networks: foo")
        )


class TestClusterProtocol_AddChassis(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.AddChassis.commandName)
        self.assertIsNotNone(responder)

    def test_chassis_type_virsh_calls_probe_virsh_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        prefix_filter = factory.make_name("prefix_filter")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "virsh",
                "hostname": hostname,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "prefix_filter": prefix_filter,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_virsh_and_enlist,
                user,
                hostname,
                password,
                prefix_filter,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_powerkvm_calls_probe_virsh_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        prefix_filter = factory.make_name("prefix_filter")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "powerkvm",
                "hostname": hostname,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "prefix_filter": prefix_filter,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_virsh_and_enlist,
                user,
                hostname,
                password,
                prefix_filter,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_virsh_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        prefix_filter = factory.make_name("prefix_filter")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "powerkvm",
                "hostname": hostname,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "prefix_filter": prefix_filter,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s", "virsh", fake_error
            ),
        )

    def test_chassis_type_vmware_calls_probe_vmware_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        prefix_filter = factory.make_name("prefix_filter")
        port = random.choice([80, 443, 8080, 8443])
        protocol = random.choice(["http", "https"])
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "vmware",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "prefix_filter": prefix_filter,
                "port": port,
                "protocol": protocol,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_vmware_and_enlist,
                user,
                hostname,
                username,
                password,
                port,
                protocol,
                prefix_filter,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_vmware_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        prefix_filter = factory.make_name("prefix_filter")
        port = random.choice([80, 443, 8080, 8443])
        protocol = random.choice(["http", "https"])
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "vmware",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "prefix_filter": prefix_filter,
                "port": port,
                "protocol": protocol,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s", "VMware", fake_error
            ),
        )

    def test_chassis_type_recs_calls_probe_and_enlist_recs(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        port = randint(2000, 4000)
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "recs_box",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "port": port,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_and_enlist_recs,
                user,
                hostname,
                port,
                username,
                password,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_recs_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        port = randint(2000, 4000)
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "recs_box",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "port": port,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s",
                "RECS|Box",
                fake_error,
            ),
        )

    def test_chassis_type_seamicro15k_calls_probe_seamicro15k_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        power_control = random.choice(["ipmi", "restapi", "restapi2"])
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "seamicro15k",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "power_control": power_control,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_seamicro15k_and_enlist,
                user,
                hostname,
                username,
                password,
                power_control,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_seamicro15k_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        power_control = random.choice(["ipmi", "restapi", "restapi2"])
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "seamicro15k",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "power_control": power_control,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s",
                "SeaMicro 15000",
                fake_error,
            ),
        )

    def test_chassis_type_mscm_calls_probe_mscm_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "mscm",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_and_enlist_mscm,
                user,
                hostname,
                username,
                password,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_mscm_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "mscm",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s",
                "Moonshot",
                fake_error,
            ),
        )

    def test_chassis_type_msftocs_calls_probe_msftocs_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        port = randint(2000, 4000)
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "msftocs",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "port": port,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_and_enlist_msftocs,
                user,
                hostname,
                port,
                username,
                password,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_msftocs_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        port = randint(2000, 4000)
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "msftocs",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
                "port": port,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s",
                "MicrosoftOCS",
                fake_error,
            ),
        )

    def test_chassis_type_ucsm_calls_probe_ucsm_and_enlist(self):
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "ucsm",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
            },
        )
        self.assertThat(
            mock_deferToThread,
            MockCalledOnceWith(
                clusterservice.probe_and_enlist_ucsm,
                user,
                hostname,
                username,
                password,
                accept_all,
                domain,
            ),
        )

    def test_chassis_type_ucsm_logs_error_to_maaslog(self):
        fake_error = factory.make_name("error")
        self.patch(clusterservice, "maaslog")
        mock_deferToThread = self.patch_autospec(
            clusterservice, "deferToThread"
        )
        mock_deferToThread.return_value = fail(Exception(fake_error))
        user = factory.make_name("user")
        hostname = factory.make_hostname()
        username = factory.make_name("username")
        password = factory.make_name("password")
        accept_all = factory.pick_bool()
        domain = factory.make_name("domain")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "ucsm",
                "hostname": hostname,
                "username": username,
                "password": password,
                "accept_all": accept_all,
                "domain": domain,
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall(
                "Failed to probe and enlist %s nodes: %s", "UCS", fake_error
            ),
        )

    def test_chassis_type_unknown_logs_error_to_maaslog(self):
        self.patch(clusterservice, "maaslog")
        user = factory.make_name("user")
        chassis_type = factory.make_name("chassis_type")
        call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": chassis_type,
                "hostname": factory.make_hostname(),
            },
        )
        self.assertThat(
            clusterservice.maaslog.error,
            MockAnyCall("Unknown chassis type %s" % chassis_type),
        )

    def test_returns_nothing(self):
        self.patch_autospec(clusterservice, "deferToThread")
        user = factory.make_name("user")
        response = call_responder(
            Cluster(),
            cluster.AddChassis,
            {
                "user": user,
                "chassis_type": "virsh",
                "hostname": factory.make_hostname(),
            },
        )
        self.assertEquals({}, response.result)


class TestClusterProtocol_DiscoverPod(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.DiscoverPod.commandName)
        self.assertIsNotNone(responder)

    def test_calls_discover_pod(self):
        mock_discover_pod = self.patch_autospec(pods, "discover_pod")
        mock_discover_pod.return_value = succeed(
            {
                "pod": DiscoveredPod(
                    architectures=["amd64/generic"],
                    cores=random.randint(1, 8),
                    cpu_speed=random.randint(1000, 3000),
                    memory=random.randint(1024, 8192),
                    local_storage=0,
                    hints=DiscoveredPodHints(
                        cores=random.randint(1, 8),
                        cpu_speed=random.randint(1000, 2000),
                        memory=random.randint(1024, 8192),
                        local_storage=0,
                    ),
                    machines=[],
                )
            }
        )
        pod_type = factory.make_name("pod_type")
        context = {"data": factory.make_name("data")}
        pod_id = random.randint(1, 100)
        name = factory.make_name("pod")
        call_responder(
            Cluster(),
            cluster.DiscoverPod,
            {
                "type": pod_type,
                "context": context,
                "pod_id": pod_id,
                "name": name,
            },
        )
        self.assertThat(
            mock_discover_pod,
            MockCalledOnceWith(pod_type, context, pod_id=pod_id, name=name),
        )


class TestClusterProtocol_ComposeMachine(MAASTestCase):
    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.ComposeMachine.commandName
        )
        self.assertIsNotNone(responder)

    def test_calls_compose_machine(self):
        mock_compose_machine = self.patch_autospec(pods, "compose_machine")
        mock_compose_machine.return_value = succeed(
            {
                "machine": DiscoveredMachine(
                    hostname=factory.make_name("hostname"),
                    architecture="amd64/generic",
                    cores=random.randint(1, 8),
                    cpu_speed=random.randint(1000, 3000),
                    memory=random.randint(1024, 8192),
                    block_devices=[],
                    interfaces=[],
                ),
                "hints": DiscoveredPodHints(
                    cores=random.randint(1, 8),
                    cpu_speed=random.randint(1000, 2000),
                    memory=random.randint(1024, 8192),
                    local_storage=0,
                ),
            }
        )
        pod_type = factory.make_name("pod_type")
        context = {"data": factory.make_name("data")}
        request = RequestedMachine(
            hostname=factory.make_name("hostname"),
            architecture="amd64/generic",
            cores=random.randint(1, 8),
            cpu_speed=random.randint(1000, 3000),
            memory=random.randint(1024, 8192),
            block_devices=[
                RequestedMachineBlockDevice(size=random.randint(8, 16))
            ],
            interfaces=[RequestedMachineInterface()],
        )
        pod_id = random.randint(1, 100)
        name = factory.make_name("pod")
        call_responder(
            Cluster(),
            cluster.ComposeMachine,
            {
                "type": pod_type,
                "context": context,
                "request": request,
                "pod_id": pod_id,
                "name": name,
            },
        )
        self.assertThat(
            mock_compose_machine,
            MockCalledOnceWith(
                pod_type, context, request, pod_id=pod_id, name=name
            ),
        )


class TestClusterProtocol_DecomposeMachine(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.DecomposeMachine.commandName
        )
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test_calls_decompose_machine(self):
        mock_decompose_machine = self.patch_autospec(pods, "decompose_machine")
        mock_decompose_machine.return_value = succeed(
            {
                "hints": DiscoveredPodHints(
                    cores=1, cpu_speed=2, memory=3, local_storage=4
                )
            }
        )
        pod_type = factory.make_name("pod_type")
        context = {"data": factory.make_name("data")}
        pod_id = random.randint(1, 100)
        name = factory.make_name("pod")
        yield call_responder(
            Cluster(),
            cluster.DecomposeMachine,
            {
                "type": pod_type,
                "context": context,
                "pod_id": pod_id,
                "name": name,
            },
        )
        self.assertThat(
            mock_decompose_machine,
            MockCalledOnceWith(pod_type, context, pod_id=pod_id, name=name),
        )


class TestClusterProtocol_DisableAndShutoffRackd(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(
            cluster.DisableAndShutoffRackd.commandName
        )
        self.assertIsNotNone(responder)

    def test_issues_restart_systemd(self):
        mock_call_and_check = self.patch(clusterservice, "call_and_check")
        response = call_responder(
            Cluster(), cluster.DisableAndShutoffRackd, {}
        )
        self.assertEquals({}, response.result)
        self.assertEquals(2, mock_call_and_check.call_count)

    def test_issues_restart_snap(self):
        self.patch(clusterservice, "running_in_snap").return_value = True
        self.patch(clusterservice, "get_snap_path").return_value = "/"
        mock_call_and_check = self.patch(clusterservice, "call_and_check")
        response = call_responder(
            Cluster(), cluster.DisableAndShutoffRackd, {}
        )
        self.assertEquals({}, response.result)
        self.assertEquals(1, mock_call_and_check.call_count)

    @inlineCallbacks
    def test_raises_error_on_failure_systemd(self):
        mock_call_and_check = self.patch(clusterservice, "call_and_check")
        mock_call_and_check.side_effect = ExternalProcessError(
            1, "systemctl", "failure"
        )
        with ExpectedException(exceptions.CannotDisableAndShutoffRackd):
            yield call_responder(Cluster(), cluster.DisableAndShutoffRackd, {})

    @inlineCallbacks
    def test_raises_error_on_failure_snap(self):
        mock_call_and_check = self.patch(clusterservice, "call_and_check")
        mock_call_and_check.side_effect = ExternalProcessError(
            random.randint(1, 255), "command-maas.wrapper", "failure"
        )
        with ExpectedException(exceptions.CannotDisableAndShutoffRackd):
            yield call_responder(Cluster(), cluster.DisableAndShutoffRackd, {})

    def test_snap_ignores_signal_error_code_on_restart(self):
        self.patch(clusterservice, "running_in_snap").return_value = True
        self.patch(clusterservice, "get_snap_path").return_value = "/"
        mock_call_and_check = self.patch(clusterservice, "call_and_check")
        mock_call_and_check.side_effect = ExternalProcessError(
            -15, "command-maas.wrapper", "failure"
        )
        response = call_responder(
            Cluster(), cluster.DisableAndShutoffRackd, {}
        )
        self.assertEquals({}, response.result)
        self.assertEquals(1, mock_call_and_check.call_count)


class TestClusterProtocol_CheckIPs(MAASTestCaseThatWaitsForDeferredThreads):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test__is_registered(self):
        protocol = Cluster()
        responder = protocol.locateResponder(cluster.CheckIPs.commandName)
        self.assertIsNotNone(responder)

    @inlineCallbacks
    def test__reports_results(self):
        ip_addresses = [
            {
                # Always exists, returns exit code of `0`.
                "ip_address": "127.0.0.1"
            },
            {
                # Broadcast that `ping` by default doesn't allow ping to
                # occur so the command returns exit code of `2`.
                "ip_address": "255.255.255.255"
            },
        ]

        # Fake `find_mac_via_arp` so its always returns a MAC address.
        fake_mac = factory.make_mac_address()
        mock_find_mac_via_arp = self.patch(clusterservice, "find_mac_via_arp")
        mock_find_mac_via_arp.return_value = fake_mac

        result = yield call_responder(
            Cluster(), cluster.CheckIPs, {"ip_addresses": ip_addresses}
        )

        self.assertThat(
            result,
            MatchesDict(
                {
                    "ip_addresses": MatchesListwise(
                        [
                            MatchesDict(
                                {
                                    "ip_address": Equals("127.0.0.1"),
                                    "used": Is(True),
                                    "interface": Is(None),
                                    "mac_address": Equals(fake_mac),
                                }
                            ),
                            MatchesDict(
                                {
                                    "ip_address": Equals("255.255.255.255"),
                                    "used": Is(False),
                                    "interface": Is(None),
                                    "mac_address": Is(None),
                                }
                            ),
                        ]
                    )
                }
            ),
        )
