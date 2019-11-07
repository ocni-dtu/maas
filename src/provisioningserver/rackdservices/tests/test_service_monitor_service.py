# Copyright 2015-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for
:py:module:`~provisioningserver.rackdservices.service_monitor_service`."""

__all__ = []

import random
from unittest.mock import Mock, sentinel

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith, MockNotCalled
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from maastesting.twisted import TwistedLoggerFixture
from provisioningserver.rackdservices import service_monitor_service as sms
from provisioningserver.rpc import getRegionClient, region
from provisioningserver.rpc.testing import MockLiveClusterToRegionRPCFixture
from provisioningserver.service_monitor import service_monitor
from provisioningserver.utils.service_monitor import (
    AlwaysOnService,
    SERVICE_STATE,
    ServiceState,
    ToggleableService,
)
from testtools.matchers import MatchesStructure
from twisted.internet.defer import fail, inlineCallbacks, succeed
from twisted.internet.task import Clock


class TestServiceMonitorService(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def setUp(self):
        super(TestServiceMonitorService, self).setUp()
        # Reset the all the toggleable services to off.
        for service in service_monitor._services.values():
            if isinstance(service, ToggleableService):
                service.off()

    def pick_service(self):
        return random.choice(list(service_monitor._services.values()))

    def patch_rpc_methods(self):
        fixture = self.useFixture(MockLiveClusterToRegionRPCFixture())
        protocol, connecting = fixture.makeEventLoop(region.UpdateServices)
        return protocol, connecting

    def test_init_sets_up_timer_correctly(self):
        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, sentinel.clock
        )
        self.assertThat(
            monitor_service,
            MatchesStructure.byEquality(
                call=(monitor_service.monitorServices, (), {}),
                step=30,
                client_service=sentinel.client_service,
                clock=sentinel.clock,
            ),
        )

    def test_monitorServices_does_not_do_anything_in_dev_environment(self):
        # Belt-n-braces make sure we're in a development environment.
        self.assertTrue(sms.is_dev_environment())

        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, Clock()
        )
        mock_ensureServices = self.patch(service_monitor, "ensureServices")
        with TwistedLoggerFixture() as logger:
            monitor_service.monitorServices()
        self.assertThat(mock_ensureServices, MockNotCalled())
        self.assertDocTestMatches(
            "Skipping check of services; they're not running under the "
            "supervision of systemd.",
            logger.output,
        )

    def test_monitorServices_calls_ensureServices(self):
        # Pretend we're in a production environment.
        self.patch(sms, "is_dev_environment").return_value = False

        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, Clock()
        )
        mock_client = Mock()
        self.patch(monitor_service, "_getConnection").return_value = succeed(
            mock_client
        )
        mock_ensureServices = self.patch(service_monitor, "ensureServices")
        monitor_service.monitorServices()
        self.assertThat(mock_ensureServices, MockCalledOnceWith())

    def test_monitorServices_handles_failure(self):
        # Pretend we're in a production environment.
        self.patch(sms, "is_dev_environment").return_value = False

        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, Clock()
        )
        mock_ensureServices = self.patch(monitor_service, "_getConnection")
        mock_ensureServices.return_value = fail(factory.make_exception())
        with TwistedLoggerFixture() as logger:
            monitor_service.monitorServices()
        self.assertDocTestMatches(
            """\
            Failed to monitor services and update region.
            Traceback (most recent call last):
            ...""",
            logger.output,
        )

    @inlineCallbacks
    def test_reports_services_to_region_on_start(self):
        # Pretend we're in a production environment.
        self.patch(sms, "is_dev_environment").return_value = False

        protocol, connecting = self.patch_rpc_methods()
        self.addCleanup((yield connecting))

        class ExampleService(AlwaysOnService):
            name = service_name = snap_service_name = factory.make_name(
                "service"
            )

        service = ExampleService()
        # Inveigle this new service into the service monitor.
        self.addCleanup(service_monitor._services.pop, service.name)
        service_monitor._services[service.name] = service

        state = ServiceState(SERVICE_STATE.ON, "running")
        mock_ensureServices = self.patch(service_monitor, "ensureServices")
        mock_ensureServices.return_value = succeed({service.name: state})

        client = getRegionClient()
        rpc_service = Mock()
        rpc_service.getClientNow.return_value = succeed(client)
        monitor_service = sms.ServiceMonitorService(rpc_service, Clock())
        yield monitor_service.startService()
        yield monitor_service.stopService()

        expected_services = list(monitor_service.ALWAYS_RUNNING_SERVICES)
        expected_services.append(
            {"name": service.name, "status": "running", "status_info": ""}
        )
        self.assertThat(
            protocol.UpdateServices,
            MockCalledOnceWith(
                protocol,
                system_id=client.localIdent,
                services=expected_services,
            ),
        )

    @inlineCallbacks
    def test_reports_services_to_region(self):
        # Pretend we're in a production environment.
        self.patch(sms, "is_dev_environment").return_value = False

        protocol, connecting = self.patch_rpc_methods()
        self.addCleanup((yield connecting))

        class ExampleService(AlwaysOnService):
            name = service_name = snap_service_name = factory.make_name(
                "service"
            )

        service = ExampleService()
        # Inveigle this new service into the service monitor.
        self.addCleanup(service_monitor._services.pop, service.name)
        service_monitor._services[service.name] = service

        state = ServiceState(SERVICE_STATE.ON, "running")
        mock_ensureServices = self.patch(service_monitor, "ensureServices")
        mock_ensureServices.return_value = succeed({service.name: state})

        client = getRegionClient()
        rpc_service = Mock()
        rpc_service.getClientNow.return_value = succeed(client)
        monitor_service = sms.ServiceMonitorService(rpc_service, Clock())

        yield monitor_service.startService()
        yield monitor_service.stopService()

        expected_services = list(monitor_service.ALWAYS_RUNNING_SERVICES)
        expected_services.append(
            {"name": service.name, "status": "running", "status_info": ""}
        )
        self.assertThat(
            protocol.UpdateServices,
            MockCalledOnceWith(
                protocol,
                system_id=client.localIdent,
                services=expected_services,
            ),
        )

    @inlineCallbacks
    def test__buildServices_includes_always_running_services(self):
        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, Clock()
        )
        observed_services = yield monitor_service._buildServices({})
        self.assertEquals(
            monitor_service.ALWAYS_RUNNING_SERVICES, observed_services
        )

    @inlineCallbacks
    def test__buildServices_adds_services_to_always_running_services(self):
        monitor_service = sms.ServiceMonitorService(
            sentinel.client_service, Clock()
        )
        service = self.pick_service()
        state = ServiceState(SERVICE_STATE.ON, "running")
        observed_services = yield monitor_service._buildServices(
            {service.name: state}
        )
        expected_services = list(monitor_service.ALWAYS_RUNNING_SERVICES)
        expected_services.append(
            {"name": service.name, "status": "running", "status_info": ""}
        )
        self.assertEquals(expected_services, observed_services)
