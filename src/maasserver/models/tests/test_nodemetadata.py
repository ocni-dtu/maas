# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver NodeMetadata model."""

__all__ = []

from crochet import wait_for
from django.core.exceptions import ValidationError
from maasserver.models import NodeMetadata
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestNodeMetadata(MAASServerTestCase):
    def test_str(self):
        # A NodeMetadata object string representation references the parent
        # node hostname.
        node = factory.make_Machine(hostname="foobar")
        entry = factory.make_NodeMetadata(node=node, key="key")
        self.assertEqual("NodeMetadata (foobar/key)", str(entry))

    def test_unique_on_node_and_key(self):
        # We can only ever have one NodeMetadata object for a particular node
        # and key.
        entry = factory.make_NodeMetadata()
        self.assertRaises(
            ValidationError,
            factory.make_NodeMetadata,
            node=entry.node,
            key=entry.key,
        )

    def test_multiple_keys_on_node(self):
        # We can only ever have one NodeMetadata object for a particular node
        # and key.
        entry1 = factory.make_NodeMetadata(key="key1", value="value")
        entry2 = factory.make_NodeMetadata(
            node=entry1.node, key="key2", value="value"
        )
        self.assertNotEqual(entry1, entry2)

    def test_get(self):
        node = factory.make_Node()
        key = factory.make_name("key")
        default = factory.make_name("default")
        self.assertEqual(
            default,
            NodeMetadata.objects.get(node=node, key=key, default=default),
        )
