# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the UbuntuOS module."""

__all__ = []

from itertools import product
import random

from distro_info import UbuntuDistroInfo
from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver.drivers.osystem import BOOT_IMAGE_PURPOSE
from provisioningserver.drivers.osystem.ubuntu import UbuntuOS


class TestUbuntuOS(MAASTestCase):
    def get_lts_release(self):
        # XXX ltrager 2018-01-08 - Force the default to be bionic before
        # bionic release for testing with MAAS 2.4.0.
        # return UbuntuDistroInfo().lts()
        return "bionic"

    def get_release_title(self, release):
        info = UbuntuDistroInfo()
        for row in info._avail(info._date):
            row_dict = row
            if not isinstance(row, dict):
                row_dict = row.__dict__
            if row_dict["series"] == release:
                return info._format("fullname", row)
        return None

    def test_get_boot_image_purposes(self):
        osystem = UbuntuOS()
        archs = [factory.make_name("arch") for _ in range(2)]
        subarchs = [factory.make_name("subarch") for _ in range(2)]
        releases = [factory.make_name("release") for _ in range(2)]
        labels = [factory.make_name("label") for _ in range(2)]
        for arch, subarch, release, label in product(
            archs, subarchs, releases, labels
        ):
            expected = osystem.get_boot_image_purposes(
                arch, subarchs, release, label
            )
            self.assertIsInstance(expected, list)
            self.assertEqual(
                expected,
                [
                    BOOT_IMAGE_PURPOSE.COMMISSIONING,
                    BOOT_IMAGE_PURPOSE.INSTALL,
                    BOOT_IMAGE_PURPOSE.XINSTALL,
                    BOOT_IMAGE_PURPOSE.DISKLESS,
                ],
            )

    def test_is_release_supported(self):
        osystem = UbuntuOS()
        info = UbuntuDistroInfo()
        self.assertTrue(osystem.is_release_supported(random.choice(info.all)))

    def test_get_lts_release(self):
        # Canary so we know when the lts changes
        osystem = UbuntuOS()
        self.assertEquals("bionic", osystem.get_lts_release())

    def test_get_default_release(self):
        osystem = UbuntuOS()
        expected = osystem.get_default_release()
        self.assertEqual(expected, self.get_lts_release())

    def test_get_supported_commissioning_releases(self):
        self.patch_autospec(UbuntuDistroInfo, "is_lts").return_value = True
        self.patch_autospec(UbuntuDistroInfo, "supported").return_value = [
            "precise",
            "trusty",
            "vivid",
            "wily",
            "xenial",
        ]
        osystem = UbuntuOS()
        releases = osystem.get_supported_commissioning_releases()
        self.assertIsInstance(releases, list)
        self.assertSequenceEqual(["vivid", "wily", "xenial"], releases)

    def test_get_supported_commissioning_releases_excludes_non_lts(self):
        supported = ["precise", "trusty", "vivid", "wily", "xenial"]
        self.patch_autospec(
            UbuntuDistroInfo, "supported"
        ).return_value = supported
        osystem = UbuntuOS()
        releases = osystem.get_supported_commissioning_releases()
        self.assertIsInstance(releases, list)
        udi = UbuntuDistroInfo()
        non_lts_releases = [name for name in supported if not udi.is_lts(name)]
        for release in non_lts_releases:
            self.assertNotIn(release, releases)

    def test_get_supported_commissioning_releases_excludes_deprecated(self):
        """Make sure we remove 'precise' from the list."""
        self.patch_autospec(UbuntuDistroInfo, "supported").return_value = [
            "precise",
            "trusty",
            "vivid",
            "wily",
            "xenial",
        ]
        osystem = UbuntuOS()
        releases = osystem.get_supported_commissioning_releases()
        self.assertIsInstance(releases, list)
        self.assertNotIn("precise", releases)
        self.assertNotIn("trusty", releases)

    def test_get_supported_commissioning_releases_excludes_unsupported_lts(
        self,
    ):
        self.patch_autospec(UbuntuDistroInfo, "supported").return_value = [
            "precise",
            "trusty",
            "vivid",
            "wily",
            "xenial",
        ]
        unsupported = [
            "warty",
            "hoary",
            "breezy",
            "dapper",
            "edgy",
            "feisty",
            "gutsy",
            "hardy",
            "intrepid",
            "jaunty",
            "karmic",
            "lucid",
            "maverick",
            "natty",
            "oneiric",
            "quantal",
            "raring",
            "saucy",
            "utopic",
        ]
        self.patch_autospec(
            UbuntuDistroInfo, "unsupported"
        ).return_value = unsupported
        osystem = UbuntuOS()
        releases = osystem.get_supported_commissioning_releases()
        self.assertIsInstance(releases, list)
        for release in unsupported:
            self.assertNotIn(release, releases)

    def test_default_commissioning_release(self):
        osystem = UbuntuOS()
        expected = osystem.get_default_commissioning_release()
        self.assertEqual(expected, self.get_lts_release())

    def test_get_release_title(self):
        osystem = UbuntuOS()
        info = UbuntuDistroInfo()
        release = random.choice(info.all)
        self.assertEqual(
            osystem.get_release_title(release), self.get_release_title(release)
        )
