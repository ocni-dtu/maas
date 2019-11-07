# Copyright 2016-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`~provisioningserver.rpc.pod`."""

__all__ = []

import random
import re
from unittest.mock import MagicMock

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.drivers.pod import (
    DiscoveredMachine,
    DiscoveredPod,
    DiscoveredPodHints,
    RequestedMachine,
    RequestedMachineBlockDevice,
    RequestedMachineInterface,
)
from provisioningserver.drivers.pod.registry import PodDriverRegistry
from provisioningserver.rpc import exceptions, pods
from testtools import ExpectedException
from twisted.internet.defer import fail, inlineCallbacks, succeed


class TestDiscoverPod(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    @inlineCallbacks
    def test_unknown_pod_raises_UnknownPodType(self):
        unknown_type = factory.make_name("unknown")
        with ExpectedException(exceptions.UnknownPodType):
            yield pods.discover_pod(unknown_type, {})

    @inlineCallbacks
    def test_handles_driver_not_returning_Deferred(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.discover.return_value = None
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'discover' did not "
                "return Deferred." % fake_driver.name
            ),
        ):
            yield pods.discover_pod(fake_driver.name, {})

    @inlineCallbacks
    def test_handles_driver_resolving_to_None(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.discover.return_value = succeed(None)
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape("unable to discover pod information."),
        ):
            yield pods.discover_pod(fake_driver.name, {})

    @inlineCallbacks
    def test_handles_driver_not_resolving_to_DiscoveredPod(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.discover.return_value = succeed({})
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'discover' returned "
                "invalid result." % fake_driver.name
            ),
        ):
            yield pods.discover_pod(fake_driver.name, {})

    @inlineCallbacks
    def test_handles_driver_resolving_to_DiscoveredPod(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        discovered_pod = DiscoveredPod(
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
        fake_driver.discover.return_value = succeed(discovered_pod)
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        result = yield pods.discover_pod(fake_driver.name, {})
        self.assertEquals({"pod": discovered_pod}, result)

    @inlineCallbacks
    def test_handles_driver_raising_NotImplementedError(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.discover.return_value = fail(NotImplementedError())
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(NotImplementedError):
            yield pods.discover_pod(fake_driver.name, {})

    @inlineCallbacks
    def test_handles_driver_raising_any_Exception(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_exception_type = factory.make_exception_type()
        fake_exception_msg = factory.make_name("error")
        fake_exception = fake_exception_type(fake_exception_msg)
        fake_driver.discover.return_value = fail(fake_exception)
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape("Failed talking to pod: " + fake_exception_msg),
        ):
            yield pods.discover_pod(fake_driver.name, {})


class TestComposeMachine(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def make_requested_machine(self):
        return RequestedMachine(
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

    @inlineCallbacks
    def test_unknown_pod_raises_UnknownPodType(self):
        unknown_type = factory.make_name("unknown")
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        with ExpectedException(exceptions.UnknownPodType):
            yield pods.compose_machine(
                unknown_type, {}, fake_request, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_handles_driver_not_returning_Deferred(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.compose.return_value = None
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'compose' did not "
                "return Deferred." % fake_driver.name
            ),
        ):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )

    @inlineCallbacks
    def test_handles_driver_resolving_to_None(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.compose.return_value = succeed(None)
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(exceptions.PodInvalidResources):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )

    @inlineCallbacks
    def test_handles_driver_not_resolving_to_tuple(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.compose.return_value = succeed({})
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'compose' returned "
                "invalid result." % fake_driver.name
            ),
        ):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )

    @inlineCallbacks
    def test_handles_driver_not_resolving_to_tuple_of_discovered(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.compose.return_value = succeed((object(), object()))
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'compose' returned "
                "invalid result." % fake_driver.name
            ),
        ):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )

    @inlineCallbacks
    def test_handles_driver_resolving_to_tuple_of_discovered(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        machine = DiscoveredMachine(
            hostname=factory.make_name("hostname"),
            architecture="amd64/generic",
            cores=random.randint(1, 8),
            cpu_speed=random.randint(1000, 3000),
            memory=random.randint(1024, 8192),
            block_devices=[],
            interfaces=[],
        )
        hints = DiscoveredPodHints(
            cores=random.randint(1, 8),
            cpu_speed=random.randint(1000, 2000),
            memory=random.randint(1024, 8192),
            local_storage=0,
        )
        fake_driver.compose.return_value = succeed((machine, hints))
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        result = yield pods.compose_machine(
            fake_driver.name, {}, fake_request, pod_id=pod_id, name=pod_name
        )
        self.assertEquals({"machine": machine, "hints": hints}, result)

    @inlineCallbacks
    def test_handles_driver_raising_NotImplementedError(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.compose.return_value = fail(NotImplementedError())
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(NotImplementedError):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )

    @inlineCallbacks
    def test_handles_driver_raising_any_Exception(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_exception_type = factory.make_exception_type()
        fake_exception_msg = factory.make_name("error")
        fake_exception = fake_exception_type(fake_exception_msg)
        fake_driver.compose.return_value = fail(fake_exception)
        fake_request = self.make_requested_machine()
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape("Failed talking to pod: " + fake_exception_msg),
        ):
            yield pods.compose_machine(
                fake_driver.name,
                {},
                fake_request,
                pod_id=pod_id,
                name=pod_name,
            )


class TestDecomposeMachine(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    @inlineCallbacks
    def test_unknown_pod_raises_UnknownPodType(self):
        unknown_type = factory.make_name("unknown")
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        with ExpectedException(exceptions.UnknownPodType):
            yield pods.decompose_machine(
                unknown_type, {}, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_handles_driver_not_returning_Deferred(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.decompose.return_value = None
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'decompose' did not "
                "return Deferred." % fake_driver.name
            ),
        ):
            yield pods.decompose_machine(
                fake_driver.name, {}, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_handles_driver_returning_None(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.decompose.return_value = succeed(None)
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'decompose' "
                "returned invalid result." % fake_driver.name
            ),
        ):
            yield pods.decompose_machine(
                fake_driver.name, {}, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_handles_driver_not_returning_hints(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.decompose.return_value = succeed(object())
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape(
                "bad pod driver '%s'; 'decompose' "
                "returned invalid result." % fake_driver.name
            ),
        ):
            yield pods.decompose_machine(
                fake_driver.name, {}, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_works_when_driver_returns_hints(self):
        hints = DiscoveredPodHints(
            cores=random.randint(1, 8),
            cpu_speed=random.randint(1000, 2000),
            memory=random.randint(1024, 8192),
            local_storage=0,
        )
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.decompose.return_value = succeed(hints)
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        result = yield pods.decompose_machine(
            fake_driver.name, {}, pod_id=pod_id, name=pod_name
        )
        self.assertEqual({"hints": hints}, result)

    @inlineCallbacks
    def test_handles_driver_raising_NotImplementedError(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_driver.decompose.return_value = fail(NotImplementedError())
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(NotImplementedError):
            yield pods.decompose_machine(
                fake_driver.name, {}, pod_id=pod_id, name=pod_name
            )

    @inlineCallbacks
    def test_handles_driver_raising_any_Exception(self):
        fake_driver = MagicMock()
        fake_driver.name = factory.make_name("pod")
        fake_exception_type = factory.make_exception_type()
        fake_exception_msg = factory.make_name("error")
        fake_exception = fake_exception_type(fake_exception_msg)
        fake_driver.decompose.return_value = fail(fake_exception)
        pod_id = random.randint(1, 10)
        pod_name = factory.make_name("pod")
        self.patch(PodDriverRegistry, "get_item").return_value = fake_driver
        with ExpectedException(
            exceptions.PodActionFail,
            re.escape("Failed talking to pod: " + fake_exception_msg),
        ):
            yield pods.decompose_machine(
                fake_driver.name, {}, pod_id=pod_id, name=pod_name
            )
