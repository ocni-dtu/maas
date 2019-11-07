# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test doubles for the region's RPC implementation."""

__all__ = ["DummyClient", "DummyClients", "HandshakingRegionServer"]

from maasserver.rpc.regionservice import RegionServer
from maastesting.factory import factory
from twisted.internet.defer import succeed


class HandshakingRegionServer(RegionServer):
    """A :class:`RegionServer` derivative that stubs ident of the cluster.

    This intercepts remote calls to `Identify` and `Authenticate` and returns
    a canned answer.

    :ivar ident: When `cluster.Identify` is called for the first time, this is
        populated with a random UUID. That UUID is also returned in the
        stub-response.
    """

    def identifyCluster(self):
        if self.ident is None:
            self.ident = factory.make_UUID()
        return succeed(None)

    def authenticateCluster(self):
        return succeed(True)


class DummyClient:
    """A dummy client that's callable, and records the UUID."""

    def __init__(self, uuid):
        self.uuid = uuid

    def __call__(self):
        raise NotImplementedError()


class DummyClients(dict):
    """Lazily hand out `DummyClient` instances."""

    def __missing__(self, uuid):
        client = DummyClient(uuid)
        self[uuid] = client
        return client
