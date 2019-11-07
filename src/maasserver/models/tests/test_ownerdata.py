# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `OwnerData`."""

__all__ = []

from maasserver.models.ownerdata import OwnerData
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase


class TestOwnerData(MAASServerTestCase):
    def get_owner_data(self, node):
        return {
            data.key: data.value
            for data in OwnerData.objects.filter(node=node)
        }

    def test_set_owner_data_adds_data(self):
        node = factory.make_Node()
        owner_data = {
            factory.make_name("key"): factory.make_name("value")
            for _ in range(3)
        }
        OwnerData.objects.set_owner_data(node, owner_data)
        self.assertEquals(owner_data, self.get_owner_data(node))

    def test_set_owner_data_updates_data(self):
        node = factory.make_Node()
        owner_data = {
            factory.make_name("key"): factory.make_name("value")
            for _ in range(3)
        }
        OwnerData.objects.set_owner_data(node, owner_data)
        for key in owner_data.keys():
            owner_data[key] = factory.make_name("value")
        OwnerData.objects.set_owner_data(node, owner_data)
        self.assertEquals(owner_data, self.get_owner_data(node))

    def test_set_owner_data_removes_data(self):
        node = factory.make_Node()
        owner_data = {
            factory.make_name("key"): factory.make_name("value")
            for _ in range(3)
        }
        OwnerData.objects.set_owner_data(node, owner_data)
        for key in owner_data.keys():
            owner_data[key] = None
        OwnerData.objects.set_owner_data(node, owner_data)
        self.assertEquals({}, self.get_owner_data(node))
