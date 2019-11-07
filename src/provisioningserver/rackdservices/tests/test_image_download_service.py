# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for provisioningserver.rackdservices.image_download_service"""

__all__ = []

from datetime import timedelta
from unittest.mock import call, Mock, sentinel
from urllib.parse import urlparse

from fixtures import FakeLogger
from maastesting.factory import factory
from maastesting.matchers import (
    get_mock_calls,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from maastesting.twisted import extract_result, TwistedLoggerFixture
from provisioningserver.boot import tftppath
from provisioningserver.rackdservices.image_download_service import (
    ImageDownloadService,
)
from provisioningserver.rpc import boot_images
from provisioningserver.rpc.boot_images import _run_import
from provisioningserver.rpc.exceptions import NoConnectionsAvailable
from provisioningserver.rpc.region import GetBootSources, GetBootSourcesV2
from twisted.application.internet import TimerService
from twisted.internet import defer
from twisted.internet.task import Clock
from twisted.protocols.amp import UnhandledCommand


class TestPeriodicImageDownloadService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_init(self):
        service = ImageDownloadService(
            sentinel.service, sentinel.tftp_root, sentinel.clock
        )
        self.assertIsInstance(service, TimerService)
        self.assertIs(service.clock, sentinel.clock)
        self.assertIs(service.client_service, sentinel.service)
        self.assertIs(service.tftp_root, sentinel.tftp_root)

    def patch_download(self, service, return_value):
        patched = self.patch(service, "_start_download")
        patched.return_value = defer.succeed(return_value)
        return patched

    def test_is_called_every_interval(self):
        clock = Clock()
        service = ImageDownloadService(
            sentinel.service, sentinel.tftp_root, clock
        )
        # Avoid actual downloads:
        self.patch_download(service, None)
        maas_meta_last_modified = self.patch(
            tftppath, "maas_meta_last_modified"
        )
        maas_meta_last_modified.return_value = None
        service.startService()

        # The first call is issued at startup.
        self.assertEqual(1, len(get_mock_calls(maas_meta_last_modified)))

        # Wind clock forward one second less than the desired interval.
        clock.advance(service.check_interval - 1)
        # No more periodic calls made.
        self.assertEqual(1, len(get_mock_calls(maas_meta_last_modified)))

        # Wind clock forward one second, past the interval.
        clock.advance(1)

        # Now there were two calls.
        self.assertEqual(2, len(get_mock_calls(maas_meta_last_modified)))

        # Forward another interval, should be three calls.
        clock.advance(service.check_interval)
        self.assertEqual(3, len(get_mock_calls(maas_meta_last_modified)))

    def test_initiates_download_if_no_meta_file(self):
        clock = Clock()
        service = ImageDownloadService(
            sentinel.service, sentinel.tftp_root, clock
        )
        _start_download = self.patch_download(service, None)
        self.patch(tftppath, "maas_meta_last_modified").return_value = None
        service.startService()
        self.assertThat(_start_download, MockCalledOnceWith())

    def test_initiates_download_if_15_minutes_has_passed(self):
        clock = Clock()
        service = ImageDownloadService(
            sentinel.service, sentinel.tftp_root, clock
        )
        _start_download = self.patch_download(service, None)
        one_week_ago = clock.seconds() - timedelta(minutes=15).total_seconds()
        self.patch(
            tftppath, "maas_meta_last_modified"
        ).return_value = one_week_ago
        service.startService()
        self.assertThat(_start_download, MockCalledOnceWith())

    def test_no_download_if_15_minutes_has_not_passed(self):
        clock = Clock()
        service = ImageDownloadService(
            sentinel.service, sentinel.tftp_root, clock
        )
        _start_download = self.patch_download(service, None)
        one_week = timedelta(minutes=15).total_seconds()
        self.patch(
            tftppath, "maas_meta_last_modified"
        ).return_value = clock.seconds()
        clock.advance(one_week - 1)
        service.startService()
        self.assertThat(_start_download, MockNotCalled())

    def test_download_is_initiated_in_new_thread(self):
        clock = Clock()
        maas_meta_last_modified = self.patch(
            tftppath, "maas_meta_last_modified"
        )
        one_week = timedelta(minutes=15).total_seconds()
        maas_meta_last_modified.return_value = clock.seconds() - one_week
        http_proxy = factory.make_simple_http_url()
        https_proxy = factory.make_simple_http_url()
        rpc_client = Mock()
        client_call = Mock()
        client_call.side_effect = [
            defer.succeed(dict(sources=sentinel.sources)),
            defer.succeed(
                dict(http=urlparse(http_proxy), https=urlparse(https_proxy))
            ),
        ]
        rpc_client.getClientNow.return_value = defer.succeed(client_call)
        rpc_client.maas_url = factory.make_simple_http_url()

        # We could patch out 'import_boot_images' instead here but I
        # don't do that for 2 reasons:
        # 1. It requires spinning the reactor again before being able to
        # test the result.
        # 2. It means there's no thread to clean up after the test.
        deferToThread = self.patch(boot_images, "deferToThread")
        deferToThread.return_value = defer.succeed(None)
        service = ImageDownloadService(rpc_client, sentinel.tftp_root, clock)
        service.startService()
        self.assertThat(
            deferToThread,
            MockCalledOnceWith(
                _run_import,
                sentinel.sources,
                rpc_client.maas_url,
                http_proxy=http_proxy,
                https_proxy=https_proxy,
            ),
        )

    def test_no_download_if_no_rpc_connections(self):
        rpc_client = Mock()
        failure = NoConnectionsAvailable()
        rpc_client.getClientNow.return_value = defer.fail(failure)

        deferToThread = self.patch(boot_images, "deferToThread")
        service = ImageDownloadService(rpc_client, self.make_dir(), Clock())
        service.startService()
        self.assertThat(deferToThread, MockNotCalled())

    def test_logs_other_errors(self):
        service = ImageDownloadService(
            sentinel.rpc, sentinel.tftp_root, Clock()
        )

        maybe_start_download = self.patch(service, "maybe_start_download")
        maybe_start_download.return_value = defer.fail(
            ZeroDivisionError("Such a shame I can't divide by zero")
        )

        with FakeLogger("maas") as maaslog, TwistedLoggerFixture() as logger:
            d = service.try_download()

        self.assertEqual(None, extract_result(d))
        self.assertDocTestMatches(
            "Failed to download images: "
            "Such a shame I can't divide by zero",
            maaslog.output,
        )
        self.assertDocTestMatches(
            """\
            Downloading images failed.
            Traceback (most recent call last):
            Failure: builtins.ZeroDivisionError: Such a shame ...
            """,
            logger.output,
        )


class TestGetBootSources(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    @defer.inlineCallbacks
    def test__get_boot_sources_calls_get_boot_sources_v2_before_v1(self):
        clock = Clock()
        client_call = Mock()
        client_call.side_effect = [
            defer.succeed(dict(sources=sentinel.sources))
        ]
        client_call.localIdent = factory.make_UUID()

        service = ImageDownloadService(sentinel.rpc, sentinel.tftp_root, clock)
        sources = yield service._get_boot_sources(client_call)
        self.assertEqual(sources.get("sources"), sentinel.sources)
        self.assertThat(
            client_call,
            MockCalledOnceWith(GetBootSourcesV2, uuid=client_call.localIdent),
        )

    @defer.inlineCallbacks
    def test__get_boot_sources_calls_get_boot_sources_v1_on_v2_missing(self):
        clock = Clock()
        client_call = Mock()
        client_call.side_effect = [
            defer.fail(UnhandledCommand()),
            defer.succeed(dict(sources=[])),
        ]
        client_call.localIdent = factory.make_UUID()

        service = ImageDownloadService(sentinel.rpc, sentinel.tftp_root, clock)
        yield service._get_boot_sources(client_call)
        self.assertThat(
            client_call,
            MockCallsMatch(
                call(GetBootSourcesV2, uuid=client_call.localIdent),
                call(GetBootSources, uuid=client_call.localIdent),
            ),
        )

    @defer.inlineCallbacks
    def test__get_boot_sources_v1_sets_os_to_wildcard(self):
        sources = [
            {
                "path": factory.make_url(),
                "selections": [
                    {
                        "release": "trusty",
                        "arches": ["amd64"],
                        "subarches": ["generic"],
                        "labels": ["release"],
                    },
                    {
                        "release": "precise",
                        "arches": ["amd64"],
                        "subarches": ["generic"],
                        "labels": ["release"],
                    },
                ],
            }
        ]

        clock = Clock()
        client_call = Mock()
        client_call.side_effect = [
            defer.fail(UnhandledCommand()),
            defer.succeed(dict(sources=sources)),
        ]

        service = ImageDownloadService(sentinel.rpc, sentinel.tftp_root, clock)
        sources = yield service._get_boot_sources(client_call)
        os_selections = [
            selection.get("os")
            for source in sources["sources"]
            for selection in source["selections"]
        ]
        self.assertEqual(["*", "*"], os_selections)
