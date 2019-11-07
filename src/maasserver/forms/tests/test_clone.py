# Copyright 2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for Clone form."""

__all__ = []

import random

from maasserver.enum import NODE_STATUS
from maasserver.forms.clone import CloneForm
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase


class TestCloneForm(MAASServerTestCase):
    def test__empty_errors(self):
        user = factory.make_admin()
        form = CloneForm(user, data={})
        self.assertFalse(form.is_valid())
        self.assertEquals(
            {
                "source": ["This field is required."],
                "destinations": ["This field is required."],
                "__all__": ["Either storage or interfaces must be true."],
            },
            form.errors,
        )

    def test__source_destination_match_error(self):
        user = factory.make_admin()
        source = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            )
        )
        form = CloneForm(
            user,
            data={
                "source": source.system_id,
                "destinations": [source.system_id],
                "storage": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertEquals(
            {
                "destinations": [
                    "Machine 0 in the array did not validate: "
                    "Source machine cannot be a destination machine."
                ]
            },
            form.errors,
        )

    def test__source_destination_smaller_storage(self):
        user = factory.make_admin()
        source = factory.make_Machine(with_boot_disk=False)
        factory.make_PhysicalBlockDevice(
            node=source, size=8 * 1024 ** 3, name="sda"
        )
        destination = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            ),
            with_boot_disk=False,
        )
        factory.make_PhysicalBlockDevice(
            node=destination, size=4 * 1024 ** 3, name="sda"
        )
        form = CloneForm(
            user,
            data={
                "source": source.system_id,
                "destinations": [destination.system_id],
                "storage": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertEquals(
            {
                "destinations": [
                    "Machine 0 in the array did not validate: "
                    "destination boot disk(sda) is smaller than "
                    "source boot disk(sda)"
                ]
            },
            form.errors,
        )

    def test__source_destination_missing_nic(self):
        user = factory.make_admin()
        source = factory.make_Machine(with_boot_disk=False)
        factory.make_Interface(node=source, name="eth0")
        destination = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            ),
            with_boot_disk=False,
        )
        factory.make_Interface(node=destination, name="eth1")
        form = CloneForm(
            user,
            data={
                "source": source.system_id,
                "destinations": [destination.system_id],
                "interfaces": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertEquals(
            {
                "destinations": [
                    "Machine 0 in the array did not validate: "
                    "destination node physical interfaces do not match "
                    "the source nodes physical interfaces: eth0"
                ]
            },
            form.errors,
        )

    def test__permission_errors(self):
        user = factory.make_User()
        source = factory.make_Machine(with_boot_disk=False)
        factory.make_PhysicalBlockDevice(
            node=source, size=8 * 1024 ** 3, name="sda"
        )
        factory.make_Interface(node=source, name="eth0")
        destination = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            ),
            with_boot_disk=False,
        )
        factory.make_PhysicalBlockDevice(
            node=destination, size=8 * 1024 ** 3, name="sda"
        )
        factory.make_Interface(node=destination, name="eth0")
        form = CloneForm(
            user,
            data={
                "source": source.system_id,
                "destinations": [destination.system_id],
                "storage": True,
                "interfaces": True,
            },
        )
        self.assertFalse(form.is_valid())
        self.assertEquals(
            {
                "destinations": [
                    "Machine 0 in the array did not validate: "
                    "Select a valid choice. %s is not one of the available "
                    "choices." % destination.system_id
                ]
            },
            form.errors,
        )

    def test__performs_clone(self):
        user = factory.make_admin()
        source = factory.make_Machine(with_boot_disk=False)
        factory.make_PhysicalBlockDevice(
            node=source, size=8 * 1024 ** 3, name="sda"
        )
        factory.make_Interface(node=source, name="eth0")
        destination1 = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            ),
            with_boot_disk=False,
        )
        factory.make_PhysicalBlockDevice(
            node=destination1, size=8 * 1024 ** 3, name="sda"
        )
        factory.make_Interface(node=destination1, name="eth0")
        destination2 = factory.make_Machine(
            status=random.choice(
                [NODE_STATUS.READY, NODE_STATUS.FAILED_TESTING]
            ),
            with_boot_disk=False,
        )
        factory.make_PhysicalBlockDevice(
            node=destination2, size=8 * 1024 ** 3, name="sda"
        )
        factory.make_Interface(node=destination2, name="eth0")
        form = CloneForm(
            user,
            data={
                "source": source.system_id,
                "destinations": [
                    destination1.system_id,
                    destination2.system_id,
                ],
                "storage": True,
                "interfaces": True,
            },
        )
        self.assertTrue(form.is_valid())
        # An exception here will cause the test to fail.
        form.save()
