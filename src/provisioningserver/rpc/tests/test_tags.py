# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module:`~provisioningserver.rpc.dhcp`."""

__all__ = []

from unittest.mock import ANY, sentinel

from apiclient.maas_client import MAASClient, MAASDispatcher, MAASOAuth
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from provisioningserver.rpc import tags


class TestEvaluateTag(MAASTestCase):
    def setUp(self):
        super(TestEvaluateTag, self).setUp()
        self.mock_url = factory.make_simple_http_url()

    def test__calls_process_node_tags(self):
        credentials = "aaa", "bbb", "ccc"
        rack_id = factory.make_name("rack")
        process_node_tags = self.patch_autospec(tags, "process_node_tags")
        tags.evaluate_tag(
            rack_id,
            [],
            sentinel.tag_name,
            sentinel.tag_definition,
            sentinel.tag_nsmap,
            credentials,
            self.mock_url,
        )
        self.assertThat(
            process_node_tags,
            MockCalledOnceWith(
                nodes=[],
                rack_id=rack_id,
                tag_name=sentinel.tag_name,
                tag_definition=sentinel.tag_definition,
                tag_nsmap=sentinel.tag_nsmap,
                client=ANY,
            ),
        )

    def test__constructs_client_with_credentials(self):
        consumer_key = factory.make_name("ckey")
        resource_token = factory.make_name("rtok")
        resource_secret = factory.make_name("rsec")
        credentials = consumer_key, resource_token, resource_secret
        rack_id = factory.make_name("rack")

        self.patch_autospec(tags, "process_node_tags")
        self.patch_autospec(tags, "MAASOAuth").side_effect = MAASOAuth

        tags.evaluate_tag(
            rack_id,
            [],
            sentinel.tag_name,
            sentinel.tag_definition,
            sentinel.tag_nsmap,
            credentials,
            self.mock_url,
        )

        client = tags.process_node_tags.call_args[1]["client"]
        self.assertIsInstance(client, MAASClient)
        self.assertEqual(self.mock_url, client.url)
        self.assertIsInstance(client.dispatcher, MAASDispatcher)
        self.assertIsInstance(client.auth, MAASOAuth)
        self.assertThat(
            tags.MAASOAuth,
            MockCalledOnceWith(consumer_key, resource_token, resource_secret),
        )
