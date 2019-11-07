# Copyright 2017-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the script API."""

__all__ = []

from base64 import b64decode, b64encode
from email.utils import format_datetime
import http.client
import json
import random

from maasserver.models import Event, VersionedTextFile
from maasserver.testing.api import APITestCase
from maasserver.testing.factory import factory
from maasserver.testing.matchers import HasStatusCode
from maasserver.utils.converters import json_load_bytes
from maasserver.utils.django_urls import reverse
from maasserver.utils.orm import reload_object
from metadataserver.enum import (
    HARDWARE_TYPE_CHOICES,
    SCRIPT_PARALLEL_CHOICES,
    SCRIPT_TYPE_CHOICES,
)
from metadataserver.models import Script
from provisioningserver.events import AUDIT
from testtools.matchers import ContainsAll


class TestScriptsAPI(APITestCase.ForUser):
    """Tests for /api/2.0/scripts/."""

    @staticmethod
    def get_scripts_uri():
        """Return the script's URI on the API."""
        return reverse("scripts_handler", args=[])

    def test_hander_path(self):
        self.assertEqual("/MAAS/api/2.0/scripts/", self.get_scripts_uri())

    def test_POST(self):
        self.become_admin()
        name = factory.make_name("script")
        title = factory.make_name("title")
        description = factory.make_name("description")
        tags = [factory.make_name("tag") for _ in range(3)]
        script_type = factory.pick_choice(SCRIPT_TYPE_CHOICES)
        hardware_type = factory.pick_choice(HARDWARE_TYPE_CHOICES)
        parallel = factory.pick_choice(SCRIPT_PARALLEL_CHOICES)
        packages = {"apt": [factory.make_name("apt_pkg")]}
        timeout = random.randint(0, 1000)
        destructive = factory.pick_bool()
        script_content = factory.make_script_content()
        comment = factory.make_name("comment")

        response = self.client.post(
            self.get_scripts_uri(),
            {
                "name": name,
                "title": title,
                "description": description,
                "tags": ",".join(tags),
                "type": script_type,
                "hardware_type": hardware_type,
                "parallel": parallel,
                "packages": json.dumps(packages),
                "timeout": timeout,
                "destructive": destructive,
                "script": factory.make_file_upload(
                    content=script_content.encode()
                ),
                "comment": comment,
            },
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_result = json_load_bytes(response.content)
        script = Script.objects.get(id=int(parsed_result["id"]))

        self.assertEquals(name, script.name)
        self.assertEquals(title, script.title)
        self.assertEquals(description, script.description)
        self.assertThat(script.tags, ContainsAll(tags))
        self.assertEquals(script_type, script.script_type)
        self.assertEquals(hardware_type, script.hardware_type)
        self.assertEquals(parallel, script.parallel)
        self.assertDictEqual(packages, script.packages)
        self.assertEquals(timeout, script.timeout.seconds)
        self.assertEquals(destructive, script.destructive)
        self.assertEquals(script_content, script.script.data)
        self.assertEquals(comment, script.script.comment)

    def test_POST_gets_name_from_filename(self):
        self.become_admin()
        name = factory.make_name("script")
        title = factory.make_name("title")
        description = factory.make_name("description")
        tags = [factory.make_name("tag") for _ in range(3)]
        script_type = factory.pick_choice(SCRIPT_TYPE_CHOICES)
        hardware_type = factory.pick_choice(HARDWARE_TYPE_CHOICES)
        parallel = factory.pick_choice(SCRIPT_PARALLEL_CHOICES)
        packages = {"apt": [factory.make_name("apt_pkg")]}
        timeout = random.randint(0, 1000)
        destructive = factory.pick_bool()
        script_content = factory.make_script_content()
        comment = factory.make_name("comment")

        response = self.client.post(
            self.get_scripts_uri(),
            {
                "title": title,
                "description": description,
                "tags": ",".join(tags),
                "type": script_type,
                "hardware_type": hardware_type,
                "parallel": parallel,
                "packages": json.dumps(packages),
                "timeout": timeout,
                "destructive": destructive,
                "comment": comment,
                name: factory.make_file_upload(name, script_content.encode()),
            },
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_result = json_load_bytes(response.content)
        script = Script.objects.get(id=int(parsed_result["id"]))

        self.assertEquals(name, script.name)
        self.assertEquals(title, script.title)
        self.assertEquals(description, script.description)
        self.assertThat(script.tags, ContainsAll(tags))
        self.assertEquals(script_type, script.script_type)
        self.assertEquals(hardware_type, script.hardware_type)
        self.assertEquals(parallel, script.parallel)
        self.assertDictEqual(packages, script.packages)
        self.assertEquals(timeout, script.timeout.seconds)
        self.assertEquals(destructive, script.destructive)
        self.assertEquals(script_content, script.script.data)
        self.assertEquals(comment, script.script.comment)

    def test_POST_requires_admin(self):
        response = self.client.post(self.get_scripts_uri())
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))

    def test_GET(self):
        scripts = [factory.make_Script() for _ in range(3)]
        response = self.client.get(self.get_scripts_uri())
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_results = json_load_bytes(response.content)

        self.assertItemsEqual(
            [script.id for script in scripts],
            [parsed_result["id"] for parsed_result in parsed_results],
        )

    def test_GET_filters_by_script_type(self):
        script = factory.make_Script()
        for _ in range(3):
            factory.make_Script(
                script_type=factory.pick_choice(
                    SCRIPT_TYPE_CHOICES, but_not=[script.script_type]
                )
            )

        response = self.client.get(
            self.get_scripts_uri(), {"type": script.script_type}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_results = json_load_bytes(response.content)

        self.assertItemsEqual(
            [script.id], [result["id"] for result in parsed_results]
        )

    def test_GET_filters_by_hardware_type(self):
        script = factory.make_Script()
        for _ in range(3):
            factory.make_Script(
                hardware_type=factory.pick_choice(
                    HARDWARE_TYPE_CHOICES, but_not=[script.hardware_type]
                )
            )

        response = self.client.get(
            self.get_scripts_uri(), {"hardware_type": script.hardware_type}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_results = json_load_bytes(response.content)

        self.assertItemsEqual(
            [script.id], [result["id"] for result in parsed_results]
        )

    def test_GET_filters(self):
        tags = [factory.make_name("tag") for _ in range(3)]
        scripts = [factory.make_Script(tags=list(tags)) for _ in range(3)]
        name_script = factory.make_Script()
        scripts.append(name_script)
        for _ in range(3):
            factory.make_Script()

        response = self.client.get(
            self.get_scripts_uri(),
            {"filters": "%s,%s" % (random.choice(tags), name_script.name)},
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_results = json_load_bytes(response.content)

        self.assertItemsEqual(
            [script.id for script in scripts],
            [parsed_result["id"] for parsed_result in parsed_results],
        )

    def test_GET_include_script(self):
        scripts = {}
        for _ in range(3):
            script = factory.make_Script()
            scripts[script.name] = script

        response = self.client.get(
            self.get_scripts_uri(), {"include_script": True}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_results = json_load_bytes(response.content)

        for result in parsed_results:
            self.assertEquals(
                scripts[result["name"]].script.data.encode(),
                b64decode(result["history"][0]["data"].encode()),
            )


class TestScriptAPI(APITestCase.ForUser):
    """Tests for /api/2.0/scripts/<script name>/."""

    scenarios = (("by_name", {"by_name": True}), ("by_id", {"by_name": False}))

    def get_script_uri(self, script):
        """Return the script's URI on the API."""
        if self.by_name:
            name = script.name
        else:
            name = script.id
        return reverse("script_handler", args=[name])

    def test_hander_path(self):
        script = factory.make_Script()
        if self.by_name:
            name = script.name
        else:
            name = script.id
        self.assertEqual(
            "/MAAS/api/2.0/scripts/%s" % name, self.get_script_uri(script)
        )

    def test_GET(self):
        script = factory.make_Script()
        script.script = script.script.update(
            new_data=factory.make_string(), comment=factory.make_string()
        )
        script.save()
        response = self.client.get(self.get_script_uri(script))
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_result = json_load_bytes(response.content)
        history = parsed_result.pop("history")

        self.assertDictEqual(
            {
                "id": script.id,
                "name": script.name,
                "title": script.title,
                "description": script.description,
                "tags": script.tags,
                "type": script.script_type,
                "type_name": script.script_type_name,
                "hardware_type": script.hardware_type,
                "hardware_type_name": script.hardware_type_name,
                "parallel": script.parallel,
                "parallel_name": script.parallel_name,
                "results": script.results,
                "parameters": script.parameters,
                "packages": script.packages,
                "timeout": str(script.timeout),
                "destructive": script.destructive,
                "for_hardware": script.for_hardware,
                "may_reboot": script.may_reboot,
                "recommission": script.recommission,
                "default": script.default,
                "apply_configured_networking": script.apply_configured_networking,
                "resource_uri": "/MAAS/api/2.0/scripts/%s" % script.name,
            },
            parsed_result,
        )
        self.assertItemsEqual(
            [rev.id for rev in script.script.previous_versions()],
            [rev["id"] for rev in history],
        )
        for result_rev in history:
            for rev in script.script.previous_versions():
                if rev.id == result_rev["id"]:
                    self.assertEquals(
                        {
                            "id": rev.id,
                            "comment": rev.comment,
                            "created": format_datetime(rev.created),
                        },
                        result_rev,
                    )

    def test_GET_include_script(self):
        script = factory.make_Script()
        script.script = script.script.update(
            new_data=factory.make_string(), comment=factory.make_string()
        )
        script.save()
        response = self.client.get(
            self.get_script_uri(script), {"include_script": True}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        parsed_result = json_load_bytes(response.content)
        history = parsed_result.pop("history")

        self.assertDictEqual(
            {
                "id": script.id,
                "name": script.name,
                "title": script.title,
                "description": script.description,
                "tags": script.tags,
                "type": script.script_type,
                "type_name": script.script_type_name,
                "hardware_type": script.hardware_type,
                "hardware_type_name": script.hardware_type_name,
                "parallel": script.parallel,
                "parallel_name": script.parallel_name,
                "results": script.results,
                "parameters": script.parameters,
                "packages": script.packages,
                "timeout": str(script.timeout),
                "destructive": script.destructive,
                "for_hardware": script.for_hardware,
                "may_reboot": script.may_reboot,
                "recommission": script.recommission,
                "default": script.default,
                "apply_configured_networking": script.apply_configured_networking,
                "resource_uri": "/MAAS/api/2.0/scripts/%s" % script.name,
            },
            parsed_result,
        )
        self.assertItemsEqual(
            [rev.id for rev in script.script.previous_versions()],
            [rev["id"] for rev in history],
        )
        for result_rev in history:
            for rev in script.script.previous_versions():
                if rev.id == result_rev["id"]:
                    self.assertEquals(
                        {
                            "id": rev.id,
                            "comment": rev.comment,
                            "created": format_datetime(rev.created),
                            "data": b64encode(rev.data.encode()).decode(),
                        },
                        result_rev,
                    )

    def test_DELETE(self):
        self.become_admin()
        script = factory.make_Script()
        response = self.client.delete(self.get_script_uri(script))
        self.assertThat(response, HasStatusCode(http.client.NO_CONTENT))
        self.assertIsNone(reload_object(script))
        event = Event.objects.get(type__level=AUDIT)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.description, "Deleted script '%s'." % script.name
        )

    def test_DELETE_admin_only(self):
        script = factory.make_Script()
        response = self.client.delete(self.get_script_uri(script))
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))
        self.assertIsNotNone(reload_object(script))

    def test_DELETE_prevents_deleting_default(self):
        self.become_admin()
        script = factory.make_Script(default=True)
        response = self.client.delete(self.get_script_uri(script))
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))
        self.assertIsNotNone(reload_object(script))

    def test_PUT(self):
        self.become_admin()
        script = factory.make_Script()
        name = factory.make_name("script")
        title = factory.make_name("title")
        description = factory.make_name("description")
        tags = [factory.make_name("tag") for _ in range(3)]
        script_type = factory.pick_choice(SCRIPT_TYPE_CHOICES)
        script_type = factory.pick_choice(SCRIPT_TYPE_CHOICES)
        hardware_type = factory.pick_choice(HARDWARE_TYPE_CHOICES)
        parallel = factory.pick_choice(SCRIPT_PARALLEL_CHOICES)
        packages = {"apt": [factory.make_name("apt_pkg")]}
        timeout = random.randint(0, 1000)
        destructive = factory.pick_bool()
        script_content = factory.make_script_content()
        comment = factory.make_name("comment")

        response = self.client.put(
            self.get_script_uri(script),
            {
                "name": name,
                "title": title,
                "description": description,
                "tags": ",".join(tags),
                "type": script_type,
                "hardware_type": hardware_type,
                "parallel": parallel,
                "packages": json.dumps(packages),
                "timeout": timeout,
                "destructive": destructive,
                "script": factory.make_file_upload(
                    content=script_content.encode()
                ),
                "comment": comment,
            },
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        script = reload_object(script)

        self.assertEquals(name, script.name)
        self.assertEquals(title, script.title)
        self.assertEquals(description, script.description)
        self.assertThat(script.tags, ContainsAll(tags))
        self.assertEquals(script_type, script.script_type)
        self.assertEquals(hardware_type, script.hardware_type)
        self.assertEquals(parallel, script.parallel)
        self.assertDictEqual(packages, script.packages)
        self.assertEquals(timeout, script.timeout.seconds)
        self.assertEquals(destructive, script.destructive)
        self.assertEquals(script_content, script.script.data)
        self.assertEquals(comment, script.script.comment)
        self.assertIsNotNone(script.script.previous_version)

    def test_PUT_gets_name_from_filename(self):
        self.become_admin()
        script = factory.make_Script()
        name = factory.make_name("script")
        title = factory.make_name("title")
        description = factory.make_name("description")
        tags = [factory.make_name("tag") for _ in range(3)]
        script_type = factory.pick_choice(SCRIPT_TYPE_CHOICES)
        hardware_type = factory.pick_choice(HARDWARE_TYPE_CHOICES)
        parallel = factory.pick_choice(SCRIPT_PARALLEL_CHOICES)
        packages = {"apt": [factory.make_name("apt_pkg")]}
        timeout = random.randint(0, 1000)
        destructive = factory.pick_bool()
        script_content = factory.make_script_content()
        comment = factory.make_name("comment")

        response = self.client.put(
            self.get_script_uri(script),
            {
                "title": title,
                "description": description,
                "tags": ",".join(tags),
                "type": script_type,
                "hardware_type": hardware_type,
                "parallel": parallel,
                "packages": json.dumps(packages),
                "timeout": timeout,
                "destructive": destructive,
                "comment": comment,
                name: factory.make_file_upload(name, script_content.encode()),
            },
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        script = reload_object(script)

        self.assertEquals(name, script.name)
        self.assertEquals(description, script.description)
        self.assertThat(script.tags, ContainsAll(tags))
        self.assertEquals(script_type, script.script_type)
        self.assertEquals(hardware_type, script.hardware_type)
        self.assertEquals(parallel, script.parallel)
        self.assertDictEqual(packages, script.packages)
        self.assertEquals(timeout, script.timeout.seconds)
        self.assertEquals(destructive, script.destructive)
        self.assertEquals(script_content, script.script.data)
        self.assertEquals(comment, script.script.comment)
        self.assertIsNotNone(script.script.previous_version)

    def test_PUT_admin_only(self):
        script = factory.make_Script()
        response = self.client.put(self.get_script_uri(script))
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))

    def test_download_gets_latest_version_by_default(self):
        script = factory.make_Script()
        script.script = script.script.update(factory.make_string())
        script.save()
        response = self.client.get(
            self.get_script_uri(script), {"op": "download"}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEquals(script.script.data, response.content.decode())

    def test_download_gets_previous_revision(self):
        script = factory.make_Script()
        script.script = script.script.update(factory.make_string())
        script.save()
        response = self.client.get(
            self.get_script_uri(script),
            {"op": "download", "revision": script.script.previous_version.id},
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEquals(
            script.script.previous_version.data, response.content.decode()
        )

    def test_download_gets_previous_rev(self):
        script = factory.make_Script()
        script.script = script.script.update(factory.make_string())
        script.save()
        response = self.client.get(
            self.get_script_uri(script),
            {"op": "download", "rev": script.script.previous_version.id},
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertEquals(
            script.script.previous_version.data, response.content.decode()
        )

    def test_download_errors_on_unknown_revision(self):
        script = factory.make_Script()
        response = self.client.get(
            self.get_script_uri(script),
            {"op": "download", "revision": random.randint(100, 1000)},
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))

    def test_revert(self):
        self.become_admin()
        script = factory.make_Script()
        textfile_ids = [script.script.id]
        for _ in range(10):
            script.script = script.script.update(factory.make_string())
            script.save()
            textfile_ids.append(script.script.id)
        revert_to = random.randint(-10, -1)
        reverted_ids = textfile_ids[revert_to:]
        remaining_ids = textfile_ids[:revert_to]
        response = self.client.post(
            self.get_script_uri(script), {"op": "revert", "to": revert_to}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        script = reload_object(script)
        self.assertEquals(
            VersionedTextFile.objects.get(id=textfile_ids[revert_to - 1]).data,
            script.script.data,
        )
        for i in reverted_ids:
            self.assertRaises(
                VersionedTextFile.DoesNotExist,
                VersionedTextFile.objects.get,
                id=i,
            )
        for i in remaining_ids:
            self.assertIsNotNone(VersionedTextFile.objects.get(id=i))
        event = Event.objects.get(type__level=AUDIT)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.description,
            "Reverted script '%s' to revision '%s'."
            % (script.name, revert_to),
        )

    def test_revert_admin_only(self):
        script = factory.make_Script()
        response = self.client.post(
            self.get_script_uri(script), {"op": "revert"}
        )
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))

    def test_revert_requires_to(self):
        self.become_admin()
        script = factory.make_Script()
        response = self.client.post(
            self.get_script_uri(script), {"op": "revert"}
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))

    def test_revert_requires_to_to_be_an_int(self):
        self.become_admin()
        script = factory.make_Script()
        to = factory.make_name("to")
        response = self.client.post(
            self.get_script_uri(script), {"op": "revert", "to": to}
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))

    def test_revert_errors_on_invalid_id(self):
        self.become_admin()
        script = factory.make_Script()
        textfile = VersionedTextFile.objects.create(data=factory.make_string())
        response = self.client.post(
            self.get_script_uri(script), {"op": "revert", "to": textfile.id}
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))

    def test_revert_prevents_reverting_default(self):
        self.become_admin()
        script = factory.make_Script(default=True)
        response = self.client.post(
            self.get_script_uri(script),
            {"op": "revert", "to": script.script.id},
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))

    def test_add_tag(self):
        self.become_admin()
        script = factory.make_Script()
        new_tag = factory.make_name("tag")
        response = self.client.post(
            self.get_script_uri(script), {"op": "add_tag", "tag": new_tag}
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertIn(new_tag, reload_object(script).tags)
        event = Event.objects.get(type__level=AUDIT)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.description,
            "Added tag '%s' to script '%s'." % (new_tag, script.name),
        )

    def test_add_tag_disallows_comma(self):
        self.become_admin()
        script = factory.make_Script()
        new_tag = "%s,%s" % (
            factory.make_name("tag"),
            factory.make_name("tag"),
        )
        response = self.client.post(
            self.get_script_uri(script), {"op": "add_tag", "tag": new_tag}
        )
        self.assertThat(response, HasStatusCode(http.client.BAD_REQUEST))
        self.assertNotIn(new_tag, reload_object(script).tags)

    def test_add_tag_admin_only(self):
        script = factory.make_Script()
        response = self.client.post(
            self.get_script_uri(script),
            {"op": "add_tag", "tag": factory.make_name("tag")},
        )
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))

    def test_remove_tag(self):
        self.become_admin()
        script = factory.make_Script(destructive=False)
        removed_tag = random.choice(
            [tag for tag in script.tags if "tag" in tag]
        )
        response = self.client.post(
            self.get_script_uri(script),
            {"op": "remove_tag", "tag": removed_tag},
        )
        self.assertThat(response, HasStatusCode(http.client.OK))
        self.assertNotIn(removed_tag, reload_object(script).tags)
        event = Event.objects.get(type__level=AUDIT)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.description,
            "Removed tag '%s' from script '%s'." % (removed_tag, script.name),
        )

    def test_remove_tag_admin_only(self):
        script = factory.make_Script(destructive=False)
        response = self.client.post(
            self.get_script_uri(script),
            {"op": "remove_tag", "tag": random.choice(script.tags)},
        )
        self.assertThat(response, HasStatusCode(http.client.FORBIDDEN))
