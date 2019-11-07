# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:mod:`maasserver.clusterrpc.utils`."""

__all__ = []

import random
from unittest.mock import Mock, sentinel

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from fixtures import FakeLogger
from maasserver.clusterrpc import utils
from maasserver.clusterrpc.utils import call_racks_synchronously
from maasserver.node_action import RPC_EXCEPTIONS
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils import asynchronous
from maastesting.matchers import (
    DocTestMatches,
    MockCalledOnceWith,
    MockNotCalled,
)
from provisioningserver.rpc.exceptions import NoConnectionsAvailable
from testtools.matchers import Equals
from twisted.python.failure import Failure


class MockFailure(Failure):
    """Fake twisted Failure object.

    Purposely doesn't call super().__init__().
    """

    def __init__(self):
        self.type = type(self)
        self.frames = []
        self.value = "Mock failure"


class TestCallClusters(MAASServerTestCase):
    """Tests for `utils.call_clusters`."""

    def test__gets_clients(self):
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.return_value = lambda: None
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = []

        # call_clusters returns with nothing because we patched out
        # asynchronous.gather, but we're interested in the side-effect:
        # getClientFor has been called for the accepted nodegroup.
        self.assertItemsEqual([], utils.call_clusters(sentinel.command))
        self.assertThat(getClientFor, MockCalledOnceWith(rack.system_id))

    def test__with_successful_callbacks(self):
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.return_value = lambda: None
        partial = self.patch(utils, "partial")
        partial.return_value = sentinel.partial
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = (
            result for result in [(sentinel.partial, sentinel.result)]
        )
        available_callback = Mock()
        unavailable_callback = Mock()
        success_callback = Mock()
        failed_callback = Mock()
        timeout_callback = Mock()
        result = list(
            utils.call_clusters(
                sentinel.command,
                available_callback=available_callback,
                unavailable_callback=unavailable_callback,
                success_callback=success_callback,
                failed_callback=failed_callback,
                timeout_callback=timeout_callback,
            )
        )
        self.assertThat(result, Equals([sentinel.result]))
        self.assertThat(available_callback, MockCalledOnceWith(rack))
        self.assertThat(unavailable_callback, MockNotCalled())
        self.assertThat(success_callback, MockCalledOnceWith(rack))
        self.assertThat(failed_callback, MockNotCalled())
        self.assertThat(timeout_callback, MockNotCalled())

    def test__with_unavailable_callbacks(self):
        logger = self.useFixture(FakeLogger("maasserver"))
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.side_effect = NoConnectionsAvailable
        partial = self.patch(utils, "partial")
        partial.return_value = sentinel.partial
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = iter([])
        available_callback = Mock()
        unavailable_callback = Mock()
        success_callback = Mock()
        failed_callback = Mock()
        timeout_callback = Mock()
        result = list(
            utils.call_clusters(
                sentinel.command,
                available_callback=available_callback,
                unavailable_callback=unavailable_callback,
                success_callback=success_callback,
                failed_callback=failed_callback,
                timeout_callback=timeout_callback,
            )
        )
        self.assertThat(result, Equals([]))
        self.assertThat(available_callback, MockNotCalled())
        self.assertThat(unavailable_callback, MockCalledOnceWith(rack))
        self.assertThat(success_callback, MockNotCalled())
        self.assertThat(failed_callback, MockNotCalled())
        self.assertThat(timeout_callback, MockNotCalled())
        self.assertThat(
            logger.output, DocTestMatches("...Unable to get RPC connection...")
        )

    def test__with_failed_callbacks(self):
        logger = self.useFixture(FakeLogger("maasserver"))
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.return_value = lambda: None
        partial = self.patch(utils, "partial")
        partial.return_value = sentinel.partial
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = (
            result for result in [(sentinel.partial, MockFailure())]
        )
        available_callback = Mock()
        unavailable_callback = Mock()
        success_callback = Mock()
        failed_callback = Mock()
        timeout_callback = Mock()
        result = list(
            utils.call_clusters(
                sentinel.command,
                available_callback=available_callback,
                unavailable_callback=unavailable_callback,
                success_callback=success_callback,
                failed_callback=failed_callback,
                timeout_callback=timeout_callback,
            )
        )
        self.assertThat(result, Equals([]))
        self.assertThat(available_callback, MockCalledOnceWith(rack))
        self.assertThat(unavailable_callback, MockNotCalled())
        self.assertThat(success_callback, MockNotCalled())
        self.assertThat(failed_callback, MockCalledOnceWith(rack))
        self.assertThat(timeout_callback, MockNotCalled())
        self.assertThat(
            logger.output,
            DocTestMatches(
                "Exception during ... on rack controller...MockFailure: ..."
            ),
        )

    def test__with_timeout_callbacks(self):
        logger = self.useFixture(FakeLogger("maasserver"))
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.return_value = lambda: None
        partial = self.patch(utils, "partial")
        partial.return_value = sentinel.partial
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = (result for result in [])
        available_callback = Mock()
        unavailable_callback = Mock()
        success_callback = Mock()
        failed_callback = Mock()
        timeout_callback = Mock()
        result = list(
            utils.call_clusters(
                sentinel.command,
                available_callback=available_callback,
                unavailable_callback=unavailable_callback,
                success_callback=success_callback,
                failed_callback=failed_callback,
                timeout_callback=timeout_callback,
            )
        )
        self.assertThat(result, Equals([]))
        self.assertThat(available_callback, MockCalledOnceWith(rack))
        self.assertThat(unavailable_callback, MockNotCalled())
        self.assertThat(success_callback, MockNotCalled())
        self.assertThat(failed_callback, MockNotCalled())
        self.assertThat(timeout_callback, MockCalledOnceWith(rack))
        self.assertThat(
            logger.output, DocTestMatches("...RPC connection timed out...")
        )


class TestCallRacksSynchronously(MAASServerTestCase):
    """Tests for `utils.call_rakcks_synchronously`."""

    def test__gets_clients(self):
        rack = factory.make_RackController()
        getClientFor = self.patch(utils, "getClientFor")
        getClientFor.return_value = lambda: None
        async_gather = self.patch(asynchronous, "gatherCallResults")
        async_gather.return_value = []

        # call_clusters returns with nothing because we patched out
        # asynchronous.gather, but we're interested in the side-effect:
        # getClientFor has been called for the accepted nodegroup.
        self.assertItemsEqual(
            [], call_racks_synchronously(sentinel.command).results
        )
        self.assertThat(getClientFor, MockCalledOnceWith(rack.system_id))


class TestGetErrorMessageForException(MAASServerTestCase):
    def test_returns_message_if_exception_has_one(self):
        error_message = factory.make_name("exception")
        self.assertEqual(
            error_message,
            utils.get_error_message_for_exception(Exception(error_message)),
        )

    def test_returns_message_if_exception_has_none(self):
        exception_class = random.choice(RPC_EXCEPTIONS)
        error_message = (
            "Unexpected exception: %s. See "
            "/var/log/maas/regiond.log "
            "on the region server for more information."
            % exception_class.__name__
        )
        self.assertEqual(
            error_message,
            utils.get_error_message_for_exception(exception_class()),
        )

    def test_returns_cluster_name_in_no_connections_error_message(self):
        rack = factory.make_RackController()
        exception = NoConnectionsAvailable(
            "Unable to connect!", uuid=rack.system_id
        )
        self.assertEqual(
            "Unable to connect to rack controller '%s' (%s); no connections "
            "available." % (rack.hostname, rack.system_id),
            utils.get_error_message_for_exception(exception),
        )

    def test_ValidationError(self):
        exception = ValidationError({NON_FIELD_ERRORS: "Some error"})
        self.assertEqual(
            utils.get_error_message_for_exception(exception), "Some error"
        )
