# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver RPC views."""

__all__ = []

import json

from crochet import wait_for
from maasserver import eventloop
from maasserver.rpc import regionservice
from maasserver.testing.eventloop import RegionEventLoopFixture
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASTransactionServerTestCase
from maasserver.utils.django_urls import reverse
from provisioningserver.utils.testing import MAASIDFixture
from testtools.matchers import (
    Equals,
    GreaterThan,
    IsInstance,
    KeysEqual,
    LessThan,
    MatchesAll,
    MatchesDict,
    MatchesListwise,
    MatchesSetwise,
)
from twisted.internet.defer import inlineCallbacks


is_valid_port = MatchesAll(IsInstance(int), GreaterThan(0), LessThan(2 ** 16))


class RPCViewTest(MAASTransactionServerTestCase):
    def setUp(self):
        super(RPCViewTest, self).setUp()
        self.maas_id = None

        def set_maas_id(maas_id):
            self.maas_id = maas_id

        self.set_maas_id = self.patch(regionservice, "set_maas_id")
        self.set_maas_id.side_effect = set_maas_id

        def get_maas_id():
            return self.maas_id

        self.get_maas_id = self.patch(regionservice, "get_maas_id")
        self.get_maas_id.side_effect = get_maas_id

    def test_rpc_info_empty(self):
        response = self.client.get(reverse("rpc-info"))
        self.assertEqual("application/json", response["Content-Type"])
        info = json.loads(response.content.decode("unicode_escape"))
        self.assertThat(info, KeysEqual("eventloops"))
        self.assertThat(info["eventloops"], MatchesDict({}))

    def test_rpc_info_from_running_ipc_master(self):
        # Run the IPC master, IPC worker, and RPC service so the endpoints
        # are updated in the database.
        region = factory.make_RegionController()
        self.useFixture(MAASIDFixture(region.system_id))
        region.owner = factory.make_admin()
        region.save()
        # `workers` is only included so ipc-master will not actually get the
        # workers service because this test runs in all-in-one mode.
        self.useFixture(
            RegionEventLoopFixture(
                "ipc-master", "ipc-worker", "rpc", "workers"
            )
        )

        eventloop.start(master=True, all_in_one=True).wait(5)
        self.addCleanup(lambda: eventloop.reset().wait(5))

        getServiceNamed = eventloop.services.getServiceNamed
        ipcMaster = getServiceNamed("ipc-master")

        @wait_for(5)
        @inlineCallbacks
        def wait_for_startup():
            # Wait for the service to complete startup.
            yield ipcMaster.starting
            yield getServiceNamed("ipc-worker").starting
            yield getServiceNamed("rpc").starting
            # Force an update, because it's very hard to track when the
            # first iteration of the ipc-master service has completed.
            yield ipcMaster.update()

        wait_for_startup()

        response = self.client.get(reverse("rpc-info"))

        self.assertEqual("application/json", response["Content-Type"])
        info = json.loads(response.content.decode("unicode_escape"))
        self.assertThat(info, KeysEqual("eventloops"))
        self.assertThat(
            info["eventloops"],
            MatchesDict(
                {
                    # Each entry in the endpoints dict is a mapping from an
                    # event loop to a list of (host, port) tuples. Each tuple is
                    # a potential endpoint for connecting into that event loop.
                    eventloop.loop.name: MatchesSetwise(
                        *(
                            MatchesListwise((Equals(addr), is_valid_port))
                            for addr, _ in ipcMaster._getListenAddresses(5240)
                        )
                    )
                }
            ),
        )
