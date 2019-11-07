# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.testing`."""

__all__ = []

import http.client

from django.db.models.signals import post_save, pre_save
from django.http import HttpResponse, HttpResponseRedirect
from maasserver.models.node import Node
from maasserver.testing import extract_redirect, NoReceivers
from maasserver.testing.factory import factory
from maasserver.testing.orm import reload_objects
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from testtools.matchers import Equals


class TestHelpers(MAASServerTestCase):
    """Test helper functions."""

    def test_extract_redirect_extracts_redirect_location(self):
        url = factory.make_string()
        self.assertEqual(url, extract_redirect(HttpResponseRedirect(url)))

    def test_extract_redirect_only_returns_target_path(self):
        url_path = factory.make_string()
        self.assertEqual(
            "/%s" % url_path,
            extract_redirect(
                HttpResponseRedirect("http://example.com/%s" % url_path)
            ),
        )

    def test_extract_redirect_errors_out_helpfully_if_not_a_redirect(self):
        content = factory.make_string(10).encode("ascii")
        other_response = HttpResponse(content=content)
        try:
            extract_redirect(other_response)
        except ValueError as e:
            self.assertThat(
                str(e),
                Equals(
                    "Not a redirect: http status %d. Content: %s"
                    % (http.client.OK.value, content)
                ),
            )

    def test_reload_object_reloads_object(self):
        test_obj = factory.make_Node()
        test_obj.save()
        Node.objects.filter(id=test_obj.id).update(hostname="newname")
        self.assertEqual("newname", reload_object(test_obj).hostname)

    def test_reload_object_returns_None_for_deleted_object(self):
        test_obj = factory.make_Node()
        test_obj.save()
        Node.objects.filter(id=test_obj.id).delete()
        self.assertIsNone(reload_object(test_obj))

    def test_reload_objects_reloads_objects(self):
        hostnames = ["name1", "name2", "name3"]
        objs = [factory.make_Node(hostname=hostname) for hostname in hostnames]
        for obj in objs:
            obj.save()
        hostnames[0] = "different"
        Node.objects.filter(id=objs[0].id).update(hostname=hostnames[0])
        self.assertItemsEqual(
            hostnames, [obj.hostname for obj in reload_objects(Node, objs)]
        )

    def test_reload_objects_omits_deleted_objects(self):
        objs = [factory.make_Node() for counter in range(3)]
        for obj in objs:
            obj.save()
        dead_obj = objs.pop(0)
        Node.objects.filter(id=dead_obj.id).delete()
        self.assertItemsEqual(objs, reload_objects(Node, objs))


class TestNoReceivers(MAASServerTestCase):
    def test_clears_and_restores_signal(self):
        # post_save already has some receivers on it, but make sure.
        self.assertNotEqual(0, len(post_save.receivers))
        old_values = list(post_save.receivers)

        with NoReceivers(post_save):
            self.assertEqual([], post_save.receivers)

        self.assertItemsEqual(old_values, post_save.receivers)

    def test_clears_and_restores_many_signals(self):
        self.assertNotEqual(0, len(post_save.receivers))
        self.assertNotEqual(0, len(pre_save.receivers))
        old_pre_values = pre_save.receivers
        old_post_values = post_save.receivers

        with NoReceivers((post_save, pre_save)):
            self.assertEqual([], post_save.receivers)
            self.assertEqual([], pre_save.receivers)

        self.assertItemsEqual(old_pre_values, pre_save.receivers)
        self.assertItemsEqual(old_post_values, post_save.receivers)

    def test_leaves_some_other_signals_alone(self):
        self.assertNotEqual(0, len(post_save.receivers))

        old_pre_values = pre_save.receivers

        with NoReceivers(post_save):
            self.assertItemsEqual(old_pre_values, pre_save.receivers)
