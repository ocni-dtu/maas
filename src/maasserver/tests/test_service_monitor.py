# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for service monitoring in the regiond."""

__all__ = []

import os

from crochet import wait_for
from maasserver.models.config import Config
from maasserver.models.signals import bootsources
from maasserver.service_monitor import ProxyService, service_monitor
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASTransactionServerTestCase
from maasserver.utils.orm import transactional
from maasserver.utils.threads import deferToDatabase
from maastesting.testcase import MAASTestCase
from provisioningserver.proxy import config
from provisioningserver.utils.service_monitor import SERVICE_STATE
from twisted.internet.defer import inlineCallbacks, maybeDeferred


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestGlobalServiceMonitor(MAASTestCase):
    def test__includes_all_services(self):
        self.assertItemsEqual(
            ["bind9", "ntp_region", "proxy", "syslog_region"],
            service_monitor._services.keys(),
        )


class TestProxyService(MAASTransactionServerTestCase):
    def make_proxy_service(self):
        class FakeProxyService(ProxyService):
            name = factory.make_name("name")
            service_name = factory.make_name("service")

        return FakeProxyService()

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_on_for_proxy_off_and_unset(self):
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

        service = self.make_proxy_service()
        yield deferToDatabase(
            transactional(Config.objects.set_config),
            "enable_http_proxy",
            False,
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config), "http_proxy", ""
        )
        self.patch(config, "is_config_present").return_value = True
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual((SERVICE_STATE.ON, None), expected_state)

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_off_for_no_config(self):
        service = self.make_proxy_service()
        os.environ["MAAS_PROXY_CONFIG_DIR"] = "/tmp/%s" % factory.make_name()
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual(
            (SERVICE_STATE.OFF, "no configuration file present."),
            expected_state,
        )
        del os.environ["MAAS_PROXY_CONFIG_DIR"]

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_on_for_proxy_off_and_set(self):
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

        service = self.make_proxy_service()
        yield deferToDatabase(
            transactional(Config.objects.set_config),
            "enable_http_proxy",
            False,
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config),
            "http_proxy",
            factory.make_url(),
        )
        self.patch(config, "is_config_present").return_value = True
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual((SERVICE_STATE.ON, None), expected_state)

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_on_for_proxy_on_but_unset(self):
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

        service = self.make_proxy_service()
        yield deferToDatabase(
            transactional(Config.objects.set_config), "enable_http_proxy", True
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config), "http_proxy", ""
        )
        self.patch(config, "is_config_present").return_value = True
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual((SERVICE_STATE.ON, None), expected_state)

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_off_for_proxy_on_and_set(self):
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

        service = self.make_proxy_service()
        yield deferToDatabase(
            transactional(Config.objects.set_config), "enable_http_proxy", True
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config),
            "http_proxy",
            factory.make_url(),
        )
        self.patch(config, "is_config_present").return_value = True
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual(
            (
                SERVICE_STATE.OFF,
                "disabled, alternate proxy is configured in settings.",
            ),
            expected_state,
        )

    @wait_for_reactor
    @inlineCallbacks
    def test_getExpectedState_returns_on_for_proxy_on_and_set_peer_proxy(self):
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

        service = self.make_proxy_service()
        yield deferToDatabase(
            transactional(Config.objects.set_config), "enable_http_proxy", True
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config), "use_peer_proxy", True
        )
        yield deferToDatabase(
            transactional(Config.objects.set_config),
            "http_proxy",
            factory.make_url(),
        )
        self.patch(config, "is_config_present").return_value = True
        expected_state = yield maybeDeferred(service.getExpectedState)
        self.assertEqual((SERVICE_STATE.ON, None), expected_state)
