# Copyright 2017-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test generation of commissioning user data."""

__all__ = []

import base64
import email
import re

from maasserver.enum import NODE_STATUS
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from metadataserver.user_data import (
    generate_user_data_for_poweroff,
    generate_user_data_for_status,
)
from testtools.matchers import ContainsAll


class TestGenerateUserData(MAASServerTestCase):
    def test_generate_user_data_produces_enlist_script(self):
        # generate_user_data produces a commissioning script which contains
        # both definitions and use of various commands in python.
        rack = factory.make_RackController()
        user_data = generate_user_data_for_status(
            None,
            NODE_STATUS.NEW,
            rack_controller=rack,
            extra_content={"enlist_commissioning": True},
        )
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        self.assertThat(
            base64.b64decode(user_data_script.get_payload()),
            ContainsAll(
                {
                    b"export DEBIAN_FRONTEND=noninteractive",
                    b"maas-run-remote-scripts",
                    b"def detect_ipmi",
                    b"class IPMIError",
                    b"def signal",
                    b"VALID_STATUS =",
                    b"def download_and_extract_tar",
                    b"COMMISSIONING",
                    b"maas-enlist",
                }
            ),
        )

    def test_generate_user_data_produces_commissioning_script(self):
        # generate_user_data produces a commissioning script which contains
        # both definitions and use of various commands in python.
        node = factory.make_Node()
        user_data = generate_user_data_for_status(
            node, status=NODE_STATUS.COMMISSIONING
        )
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        self.assertThat(
            base64.b64decode(user_data_script.get_payload()),
            ContainsAll(
                {
                    b"export DEBIAN_FRONTEND=noninteractive",
                    b"maas-run-remote-scripts",
                    b"def detect_ipmi",
                    b"class IPMIError",
                    b"def signal",
                    b"VALID_STATUS =",
                    b"def download_and_extract_tar",
                }
            ),
        )

    def test_generate_user_data_produces_testing_script(self):
        node = factory.make_Node()
        user_data = generate_user_data_for_status(
            node, status=NODE_STATUS.TESTING
        )
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        self.assertThat(
            base64.b64decode(user_data_script.get_payload()),
            ContainsAll(
                {
                    b"export DEBIAN_FRONTEND=noninteractive",
                    b"maas-run-remote-scripts",
                    b"def signal",
                    b"def download_and_extract_tar",
                }
            ),
        )

    def test_generate_user_data_produces_rescue_mode_script(self):
        node = factory.make_Node()
        user_data = generate_user_data_for_status(
            node, status=NODE_STATUS.RESCUE_MODE
        )
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        self.assertThat(
            base64.b64decode(user_data_script.get_payload()),
            ContainsAll(
                {
                    b"export DEBIAN_FRONTEND=noninteractive",
                    b"maas-run-remote-scripts",
                    b"def signal",
                    b"def download_and_extract_tar",
                }
            ),
        )

    def test_generate_user_data_produces_poweroff_script(self):
        node = factory.make_Node()
        user_data = generate_user_data_for_poweroff(node)
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        self.assertThat(
            base64.b64decode(user_data_script.get_payload()),
            ContainsAll({b"Powering node off."}),
        )


class TestDiskErasingUserData(MAASServerTestCase):

    scenarios = (
        (
            "secure_and_quick",
            {
                "extra_content": {"secure_erase": True, "quick_erase": True},
                "maas_wipe": rb"^\s*maas-wipe\s--secure-erase\s--quick-erase$\s*signal\sOK",
            },
        ),
        (
            "secure_not_quick",
            {
                "extra_content": {"secure_erase": True, "quick_erase": False},
                "maas_wipe": rb"^\s*maas-wipe\s--secure-erase\s$\s*signal\sOK",
            },
        ),
        (
            "quick_not_secure",
            {
                "extra_content": {"secure_erase": False, "quick_erase": True},
                "maas_wipe": rb"^\s*maas-wipe\s\s--quick-erase$\s*signal\sOK",
            },
        ),
        (
            "not_quick_not_secure",
            {
                "extra_content": {"secure_erase": False, "quick_erase": False},
                "maas_wipe": rb"^\s*maas-wipe\s\s$\s*signal\sOK",
            },
        ),
    )

    def test_generate_user_data_produces_disk_erase_script(self):
        node = factory.make_Node()
        user_data = generate_user_data_for_status(
            node,
            status=NODE_STATUS.DISK_ERASING,
            extra_content=self.extra_content,
        )
        parsed_data = email.message_from_string(user_data.decode("utf-8"))
        self.assertTrue(parsed_data.is_multipart())

        user_data_script = parsed_data.get_payload()[0]
        self.assertEquals(
            'text/x-shellscript; charset="utf-8"',
            user_data_script["Content-Type"],
        )
        self.assertEquals(
            "base64", user_data_script["Content-Transfer-Encoding"]
        )
        self.assertEquals(
            'attachment; filename="user_data.sh"',
            user_data_script["Content-Disposition"],
        )
        payload = base64.b64decode(user_data_script.get_payload())
        self.assertThat(
            payload,
            ContainsAll(
                {
                    b"export DEBIAN_FRONTEND=noninteractive",
                    b"maas-wipe",
                    b"def signal",
                    b"VALID_STATUS =",
                    b"class WipeError",
                }
            ),
        )
        self.assertIsNotNone(
            re.search(self.maas_wipe, payload, re.MULTILINE | re.DOTALL)
        )
