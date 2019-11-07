# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the `Boot Source Selections` API."""

__all__ = []

import http.client

from maasserver.api.boot_source_selections import (
    DISPLAYED_BOOTSOURCESELECTION_FIELDS,
)
from maasserver.models import BootSourceSelection
from maasserver.models.signals import bootsources
from maasserver.testing.api import APITestCase
from maasserver.testing.factory import factory
from maasserver.utils.converters import json_load_bytes
from maasserver.utils.django_urls import reverse
from maasserver.utils.orm import reload_object
from testtools.matchers import MatchesStructure


def get_boot_source_selection_uri(boot_source_selection):
    """Return a boot source's URI on the API."""
    boot_source = boot_source_selection.boot_source
    return reverse(
        "boot_source_selection_handler",
        args=[boot_source.id, boot_source_selection.id],
    )


class TestBootSourceSelectionAPI(APITestCase.ForUser):
    def setUp(self):
        super(TestBootSourceSelectionAPI, self).setUp()
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

    def test_handler_path(self):
        self.assertEqual(
            "/MAAS/api/2.0/boot-sources/3/selections/4/",
            reverse("boot_source_selection_handler", args=["3", "4"]),
        )

    def test_GET_returns_boot_source(self):
        self.become_admin()
        boot_source_selection = factory.make_BootSourceSelection()
        response = self.client.get(
            get_boot_source_selection_uri(boot_source_selection)
        )
        self.assertEqual(http.client.OK, response.status_code)
        returned_boot_source_selection = json_load_bytes(response.content)
        boot_source = boot_source_selection.boot_source
        # The returned object contains a 'resource_uri' field.
        self.assertEqual(
            reverse(
                "boot_source_selection_handler",
                args=[boot_source.id, boot_source_selection.id],
            ),
            returned_boot_source_selection["resource_uri"],
        )
        # The other fields are the boot source selection's fields.
        del returned_boot_source_selection["resource_uri"]
        # All the fields are present.
        self.assertItemsEqual(
            DISPLAYED_BOOTSOURCESELECTION_FIELDS,
            returned_boot_source_selection.keys(),
        )
        self.assertThat(
            boot_source_selection,
            MatchesStructure.byEquality(**returned_boot_source_selection),
        )

    def test_GET_requires_admin(self):
        boot_source_selection = factory.make_BootSourceSelection()
        response = self.client.get(
            get_boot_source_selection_uri(boot_source_selection)
        )
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_DELETE_deletes_boot_source_selection(self):
        self.become_admin()
        boot_source_selection = factory.make_BootSourceSelection()
        response = self.client.delete(
            get_boot_source_selection_uri(boot_source_selection)
        )
        self.assertEqual(http.client.NO_CONTENT, response.status_code)
        self.assertIsNone(reload_object(boot_source_selection))

    def test_DELETE_requires_admin(self):
        boot_source_selection = factory.make_BootSourceSelection()
        response = self.client.delete(
            get_boot_source_selection_uri(boot_source_selection)
        )
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_PUT_updates_boot_source_selection(self):
        self.become_admin()
        boot_source_selection = factory.make_BootSourceSelection()
        new_os = factory.make_name("os")
        new_release = factory.make_name("release")
        boot_source_caches = factory.make_many_BootSourceCaches(
            2,
            boot_source=boot_source_selection.boot_source,
            os=new_os,
            release=new_release,
        )
        new_values = {
            "os": new_os,
            "release": new_release,
            "arches": [boot_source_caches[0].arch, boot_source_caches[1].arch],
            "subarches": [
                boot_source_caches[0].subarch,
                boot_source_caches[1].subarch,
            ],
            "labels": [boot_source_caches[0].label],
        }
        response = self.client.put(
            get_boot_source_selection_uri(boot_source_selection), new_values
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        boot_source_selection = reload_object(boot_source_selection)
        self.assertAttributes(boot_source_selection, new_values)

    def test_PUT_requires_admin(self):
        boot_source_selection = factory.make_BootSourceSelection()
        new_values = {"release": factory.make_name("release")}
        response = self.client.put(
            get_boot_source_selection_uri(boot_source_selection), new_values
        )
        self.assertEqual(http.client.FORBIDDEN, response.status_code)


class TestBootSourceSelectionsAPI(APITestCase.ForUser):
    """Test the the boot source selections API."""

    def setUp(self):
        super(TestBootSourceSelectionsAPI, self).setUp()
        # Disable boot source cache signals.
        self.addCleanup(bootsources.signals.enable)
        bootsources.signals.disable()

    def test_handler_path(self):
        self.assertEqual(
            "/MAAS/api/2.0/boot-sources/3/selections/",
            reverse("boot_source_selections_handler", args=["3"]),
        )

    def test_GET_returns_boot_source_selection_list(self):
        self.become_admin()
        boot_source = factory.make_BootSource()
        selections = [
            factory.make_BootSourceSelection(boot_source=boot_source)
            for _ in range(3)
        ]
        # Create boot source selections in another boot source.
        [factory.make_BootSourceSelection() for _ in range(3)]
        response = self.client.get(
            reverse("boot_source_selections_handler", args=[boot_source.id])
        )
        self.assertEqual(
            http.client.OK, response.status_code, response.content
        )
        parsed_result = json_load_bytes(response.content)
        self.assertItemsEqual(
            [selection.id for selection in selections],
            [selection.get("id") for selection in parsed_result],
        )

    def test_GET_requires_admin(self):
        boot_source = factory.make_BootSource()
        response = self.client.get(
            reverse("boot_source_selections_handler", args=[boot_source.id])
        )
        self.assertEqual(http.client.FORBIDDEN, response.status_code)

    def test_POST_creates_boot_source_selection(self):
        self.become_admin()
        boot_source = factory.make_BootSource()
        new_release = factory.make_name("release")
        boot_source_caches = factory.make_many_BootSourceCaches(
            2, boot_source=boot_source, release=new_release
        )
        params = {
            "release": new_release,
            "arches": [boot_source_caches[0].arch, boot_source_caches[1].arch],
            "subarches": [
                boot_source_caches[0].subarch,
                boot_source_caches[1].subarch,
            ],
            "labels": [boot_source_caches[0].label],
        }
        response = self.client.post(
            reverse("boot_source_selections_handler", args=[boot_source.id]),
            params,
        )
        self.assertEqual(http.client.OK, response.status_code)
        parsed_result = json_load_bytes(response.content)

        boot_source_selection = BootSourceSelection.objects.get(
            id=parsed_result["id"]
        )
        self.assertAttributes(boot_source_selection, params)

    def test_POST_requires_admin(self):
        boot_source = factory.make_BootSource()
        new_release = factory.make_name("release")
        params = {
            "release": new_release,
            "arches": [factory.make_name("arch"), factory.make_name("arch")],
            "subarches": [
                factory.make_name("subarch"),
                factory.make_name("subarch"),
            ],
            "labels": [factory.make_name("label")],
        }
        response = self.client.post(
            reverse("boot_source_selections_handler", args=[boot_source.id]),
            params,
        )
        self.assertEqual(http.client.FORBIDDEN, response.status_code)
