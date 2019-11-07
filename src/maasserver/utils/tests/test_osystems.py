# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.utils.osystems`."""

__all__ = []

from operator import itemgetter
import random

from distro_info import UbuntuDistroInfo
from django.core.exceptions import ValidationError
from maasserver.clusterrpc.testing.osystems import (
    make_rpc_osystem,
    make_rpc_release,
)
from maasserver.enum import BOOT_RESOURCE_TYPE
from maasserver.models import BootResource, Config
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.testing.factory import factory
from maasserver.testing.osystems import make_usable_osystem
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.osystems import (
    get_distro_series_initial,
    get_release_from_db,
    get_release_from_distro_info,
    get_release_requires_key,
    get_release_version_from_string,
    list_all_releases_requiring_keys,
    list_all_usable_osystems,
    list_all_usable_releases,
    list_commissioning_choices,
    list_osystem_choices,
    list_release_choices,
    make_hwe_kernel_ui_text,
    release_a_newer_than_b,
    validate_hwe_kernel,
    validate_min_hwe_kernel,
    validate_osystem_and_distro_series,
)
from maastesting.matchers import MockAnyCall, MockCalledOnceWith


class TestOsystems(MAASServerTestCase):
    def test_list_all_usable_osystems(self):
        custom_os = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.UPLOADED,
            extra={"title": factory.make_name("title")},
        )
        factory.make_default_ubuntu_release_bootable()
        # Bootloader to be ignored.
        factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, bootloader_type="uefi"
        )

        self.assertItemsEqual(
            [
                {
                    "name": "custom",
                    "title": "Custom",
                    "default_commissioning_release": None,
                    "default_release": "",
                    "releases": [
                        {
                            "name": custom_os.name,
                            "title": custom_os.extra["title"],
                            "can_commission": False,
                            "requires_license_key": False,
                        }
                    ],
                },
                {
                    "name": "ubuntu",
                    "title": "Ubuntu",
                    "default_commissioning_release": "bionic",
                    "default_release": "bionic",
                    "releases": [
                        {
                            "name": "bionic",
                            "title": 'Ubuntu 18.04 LTS "Bionic Beaver"',
                            "can_commission": True,
                            "requires_license_key": False,
                        }
                    ],
                },
            ],
            list_all_usable_osystems(),
        )

    def test_list_osystem_choices_includes_default(self):
        self.assertEqual(
            [("", "Default OS")],
            list_osystem_choices([], include_default=True),
        )

    def test_list_osystem_choices_doesnt_include_default(self):
        self.assertEqual([], list_osystem_choices([], include_default=False))

    def test_list_osystem_choices_uses_name_and_title(self):
        osystem = make_rpc_osystem()
        self.assertEqual(
            [(osystem["name"], osystem["title"])],
            list_osystem_choices([osystem], include_default=False),
        )

    def test_list_osystem_choices_doesnt_duplicate(self):
        self.assertEqual(
            [("custom", "Custom")],
            list_osystem_choices(
                [
                    {"name": "custom", "title": "Custom"},
                    {"name": "custom", "title": "Custom"},
                ],
                include_default=False,
            ),
        )


class TestReleases(MAASServerTestCase):
    def make_release_choice(self, os_name, release, include_asterisk=False):
        key = "%s/%s" % (os_name, release["name"])
        title = release["title"]
        if not title:
            title = release["name"]
        if include_asterisk:
            return ("%s*" % key, title)
        return (key, title)

    def test_list_all_usable_releases(self):
        custom_os = factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.UPLOADED,
            extra={"title": factory.make_name("title")},
        )
        factory.make_default_ubuntu_release_bootable()
        # Bootloader to be ignored.
        factory.make_BootResource(
            rtype=BOOT_RESOURCE_TYPE.SYNCED, bootloader_type="uefi"
        )
        self.assertDictEqual(
            {
                "custom": [
                    {
                        "name": custom_os.name,
                        "title": custom_os.extra["title"],
                        "can_commission": False,
                        "requires_license_key": False,
                    }
                ],
                "ubuntu": [
                    {
                        "name": "bionic",
                        "title": 'Ubuntu 18.04 LTS "Bionic Beaver"',
                        "can_commission": True,
                        "requires_license_key": False,
                    }
                ],
            },
            dict(list_all_usable_releases()),
        )

    def test_list_all_releases_requiring_keys(self):
        releases = [
            make_rpc_release(requires_license_key=True) for _ in range(3)
        ]
        release_without_license_key = make_rpc_release(
            requires_license_key=False
        )
        osystem = make_rpc_osystem(
            releases=releases + [release_without_license_key]
        )
        self.assertItemsEqual(
            releases,
            list_all_releases_requiring_keys([osystem])[osystem["name"]],
        )

    def test_list_all_releases_requiring_keys_sorts(self):
        releases = [
            make_rpc_release(requires_license_key=True) for _ in range(3)
        ]
        release_without_license_key = make_rpc_release(
            requires_license_key=False
        )
        osystem = make_rpc_osystem(
            releases=releases + [release_without_license_key]
        )
        releases = sorted(releases, key=itemgetter("title"))
        self.assertEqual(
            releases,
            list_all_releases_requiring_keys([osystem])[osystem["name"]],
        )

    def test_get_release_requires_key_returns_asterisk_when_required(self):
        release = make_rpc_release(requires_license_key=True)
        self.assertEqual("*", get_release_requires_key(release))

    def test_get_release_requires_key_returns_empty_when_not_required(self):
        release = make_rpc_release(requires_license_key=False)
        self.assertEqual("", get_release_requires_key(release))

    def test_list_release_choices_includes_default(self):
        self.assertEqual(
            [("", "Default OS Release")],
            list_release_choices({}, include_default=True),
        )

    def test_list_release_choices_doesnt_include_default(self):
        self.assertEqual([], list_release_choices({}, include_default=False))

    def test_list_release_choices(self):
        for _ in range(3):
            factory.make_BootResource(name="custom/%s" % factory.make_name())
        choices = [
            self.make_release_choice("custom", release)
            for release in list_all_usable_releases()["custom"]
        ]
        self.assertItemsEqual(
            choices,
            list_release_choices(
                list_all_usable_releases(), include_default=False
            ),
        )

    def test_list_release_choices_fallsback_to_name(self):
        for _ in range(3):
            factory.make_BootResource(name="custom/%s" % factory.make_name())
        releases = list_all_usable_releases()["custom"]
        for release in releases:
            release["title"] = ""
        choices = [
            self.make_release_choice("custom", release) for release in releases
        ]
        self.assertItemsEqual(
            choices,
            list_release_choices(
                list_all_usable_releases(), include_default=False
            ),
        )

    def test_list_release_choices_sorts(self):
        for _ in range(3):
            factory.make_BootResource(name="custom/%s" % factory.make_name())
        releases = list_all_usable_releases()["custom"]
        choices = [
            self.make_release_choice("custom", release)
            for release in sorted(releases, key=itemgetter("title"))
        ]
        self.assertEqual(
            choices,
            list_release_choices(
                list_all_usable_releases(), include_default=False
            ),
        )

    def test_list_release_choices_includes_requires_key_asterisk(self):
        factory.make_BootResource(name="windows/win2016")
        releases = list_all_usable_releases()["windows"]
        choices = [
            self.make_release_choice("windows", release, include_asterisk=True)
            for release in releases
        ]
        self.assertItemsEqual(
            choices,
            list_release_choices(
                list_all_usable_releases(), include_default=False
            ),
        )

    def test_get_distro_series_initial(self):
        releases = [make_rpc_release() for _ in range(3)]
        osystem = make_rpc_osystem(releases=releases)
        release = random.choice(releases)
        node = factory.make_Node(
            osystem=osystem["name"], distro_series=release["name"]
        )
        self.assertEqual(
            "%s/%s" % (osystem["name"], release["name"]),
            get_distro_series_initial(
                [osystem], node, with_key_required=False
            ),
        )

    def test_get_distro_series_initial_without_key_required(self):
        releases = [
            make_rpc_release(requires_license_key=True) for _ in range(3)
        ]
        osystem = make_rpc_osystem(releases=releases)
        release = random.choice(releases)
        node = factory.make_Node(
            osystem=osystem["name"], distro_series=release["name"]
        )
        self.assertEqual(
            "%s/%s" % (osystem["name"], release["name"]),
            get_distro_series_initial(
                [osystem], node, with_key_required=False
            ),
        )

    def test_get_distro_series_initial_with_key_required(self):
        releases = [
            make_rpc_release(requires_license_key=True) for _ in range(3)
        ]
        osystem = make_rpc_osystem(releases=releases)
        release = random.choice(releases)
        node = factory.make_Node(
            osystem=osystem["name"], distro_series=release["name"]
        )
        self.assertEqual(
            "%s/%s*" % (osystem["name"], release["name"]),
            get_distro_series_initial([osystem], node, with_key_required=True),
        )

    def test_get_distro_series_initial_works_around_conflicting_os(self):
        # Test for bug 1456892.
        releases = [
            make_rpc_release(requires_license_key=True) for _ in range(3)
        ]
        osystem = make_rpc_osystem(releases=releases)
        release = random.choice(releases)
        node = factory.make_Node(
            osystem=osystem["name"], distro_series=release["name"]
        )
        self.assertEqual(
            "%s/%s" % (osystem["name"], release["name"]),
            get_distro_series_initial([], node, with_key_required=True),
        )

    def test_list_commissioning_choices_returns_empty_list_if_not_ubuntu(self):
        osystem = make_rpc_osystem()
        self.assertEqual([], list_commissioning_choices([osystem]))

    def test_list_commissioning_choices_returns_commissioning_releases(self):
        comm_releases = [
            make_rpc_release(can_commission=True) for _ in range(3)
        ]
        comm_releases += [
            make_rpc_release(
                Config.objects.get_config("commissioning_distro_series"),
                can_commission=True,
            )
        ]
        no_comm_release = make_rpc_release()
        osystem = make_rpc_osystem(
            "ubuntu", releases=comm_releases + [no_comm_release]
        )
        choices = [
            (release["name"], release["title"]) for release in comm_releases
        ]
        self.assertItemsEqual(choices, list_commissioning_choices([osystem]))

    def test_list_commissioning_choices_returns_sorted(self):
        comm_releases = [
            make_rpc_release(can_commission=True) for _ in range(3)
        ]
        comm_releases += [
            make_rpc_release(
                Config.objects.get_config("commissioning_distro_series"),
                can_commission=True,
            )
        ]
        osystem = make_rpc_osystem("ubuntu", releases=comm_releases)
        comm_releases = sorted(comm_releases, key=itemgetter("title"))
        choices = [
            (release["name"], release["title"]) for release in comm_releases
        ]
        self.assertEqual(choices, list_commissioning_choices([osystem]))

    def test_list_commissioning_choices_returns_current_selection(self):
        comm_releases = [
            make_rpc_release(can_commission=True) for _ in range(3)
        ]
        osystem = make_rpc_osystem("ubuntu", releases=comm_releases)
        comm_releases = sorted(comm_releases, key=itemgetter("title"))
        commissioning_series, _ = Config.objects.get_or_create(
            name="commissioning_distro_series"
        )
        commissioning_series.value = factory.make_name("commissioning_series")
        commissioning_series.save()
        choices = [
            (
                commissioning_series.value,
                "%s (No image available)" % commissioning_series.value,
            )
        ] + [(release["name"], release["title"]) for release in comm_releases]
        self.assertEqual(choices, list_commissioning_choices([osystem]))

    def test_make_hwe_kernel_ui_text_finds_release_from_bootsourcecache(self):
        self.useFixture(SignalsDisabled("bootsources"))
        release = factory.pick_ubuntu_release()
        kernel = "hwe-" + release[0]
        factory.make_BootSourceCache(
            os="ubuntu/%s" % release, subarch=kernel, release=release
        )
        self.assertEqual(
            "%s (%s)" % (release, kernel), make_hwe_kernel_ui_text(kernel)
        )

    def test_make_hwe_kernel_ui_finds_release_from_ubuntudistroinfo(self):
        self.assertEqual("trusty (hwe-t)", make_hwe_kernel_ui_text("hwe-t"))

    def test_make_hwe_kernel_ui_returns_kernel_when_none_found(self):
        unknown_kernel = factory.make_name("kernel")
        self.assertEqual(
            unknown_kernel, make_hwe_kernel_ui_text(unknown_kernel)
        )


class TestValidateOsystemAndDistroSeries(MAASServerTestCase):
    def test__raises_error_of_osystem_and_distro_series_dont_match(self):
        os = factory.make_name("os")
        release = "%s/%s" % (
            factory.make_name("os"),
            factory.make_name("release"),
        )
        error = self.assertRaises(
            ValidationError, validate_osystem_and_distro_series, os, release
        )
        self.assertEqual(
            "%s in distro_series does not match with "
            "operating system %s." % (release, os),
            error.message,
        )

    def test__raises_error_if_not_supported_osystem(self):
        os = factory.make_name("os")
        release = factory.make_name("release")
        error = self.assertRaises(
            ValidationError, validate_osystem_and_distro_series, os, release
        )
        self.assertEqual(
            "%s is not a support operating system." % os, error.message
        )

    def test__raises_error_if_not_supported_release(self):
        factory.make_Node()
        osystem = make_usable_osystem(self)
        release = factory.make_name("release")
        error = self.assertRaises(
            ValidationError,
            validate_osystem_and_distro_series,
            osystem["name"],
            release,
        )
        self.assertEqual(
            "%s/%s is not a support operating system and release "
            "combination." % (osystem["name"], release),
            error.message,
        )

    def test__returns_osystem_and_release_with_license_key_stripped(self):
        factory.make_Node()
        osystem = make_usable_osystem(self)
        release = osystem["default_release"]
        self.assertEqual(
            (osystem["name"], release),
            validate_osystem_and_distro_series(osystem["name"], release + "*"),
        )


class TestGetReleaseVersionFromString(MAASServerTestCase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ubuntu = UbuntuDistroInfo()
        # We can't test with releases older than Precise as they have duplicate
        # names(e.g Wily and Warty) which will break the old style kernel
        # tests.
        try:
            ubuntu_rows = ubuntu._rows
        except AttributeError:
            ubuntu_rows = [row.__dict__ for row in ubuntu._releases]
        valid_releases = [
            row
            for row in ubuntu_rows
            if int(row["version"].split(".")[0]) >= 12
        ]
        release = random.choice(valid_releases)
        # Remove 'LTS' from version if it exists
        version_str = release["version"].split(" ")[0]
        # Convert the version into a list of ints
        version_tuple = tuple([int(seg) for seg in version_str.split(".")])

        self.scenarios = (
            (
                "Release name",
                {
                    "string": release["series"],
                    "expected": version_tuple + tuple([0]),
                },
            ),
            (
                "Release version",
                {
                    "string": version_str,
                    "expected": version_tuple + tuple([0]),
                },
            ),
            (
                "Old style kernel",
                {
                    "string": "hwe-%s" % release["series"][0],
                    "expected": version_tuple + tuple([0]),
                },
            ),
            (
                "GA kernel",
                {
                    "string": "ga-%s" % version_str,
                    "expected": version_tuple + tuple([0]),
                },
            ),
            (
                "GA low latency kernel",
                {
                    "string": "ga-%s-lowlatency" % version_str,
                    "expected": version_tuple + tuple([0]),
                },
            ),
            (
                "New style kernel",
                {
                    "string": "hwe-%s" % version_str,
                    "expected": version_tuple + tuple([1]),
                },
            ),
            (
                "New style edge kernel",
                {
                    "string": "hwe-%s-edge" % version_str,
                    "expected": version_tuple + tuple([2]),
                },
            ),
            (
                "New style low latency kernel",
                {
                    "string": "hwe-%s-lowlatency" % version_str,
                    "expected": version_tuple + tuple([1]),
                },
            ),
            (
                "New style edge low latency kernel",
                {
                    "string": "hwe-%s-lowlatency-edge" % version_str,
                    "expected": version_tuple + tuple([2]),
                },
            ),
            (
                "Rolling kernel",
                {"string": "hwe-rolling", "expected": tuple([999, 999, 1])},
            ),
            (
                "Rolling edge kernel",
                {
                    "string": "hwe-rolling-edge",
                    "expected": tuple([999, 999, 2]),
                },
            ),
            (
                "Rolling lowlatency kernel",
                {
                    "string": "hwe-rolling-lowlatency",
                    "expected": tuple([999, 999, 1]),
                },
            ),
            (
                "Rolling lowlatency edge kernel",
                {
                    "string": "hwe-rolling-lowlatency-edge",
                    "expected": tuple([999, 999, 2]),
                },
            ),
        )

    def test_get_release_version_from_string(self):
        self.assertEquals(
            self.expected, get_release_version_from_string(self.string)
        )


class TestReleaseANewerThanB(MAASServerTestCase):
    def test_a_newer_than_b_true(self):
        self.assertTrue(
            release_a_newer_than_b(
                "hwe-rolling",
                factory.make_kernel_string(can_be_release_or_version=True),
            )
        )

    def test_a_equal_to_b_true(self):
        string = factory.make_kernel_string(can_be_release_or_version=True)
        self.assertTrue(release_a_newer_than_b(string, string))

    def test_a_less_than_b_false(self):
        self.assertFalse(
            release_a_newer_than_b(
                factory.make_kernel_string(can_be_release_or_version=True),
                "hwe-rolling",
            )
        )

    def test_accounts_for_edge(self):
        self.assertFalse(
            release_a_newer_than_b("hwe-rolling", "hwe-rolling-edge")
        )

    def test_kernel_flavor_doesnt_make_difference(self):
        self.assertTrue(
            release_a_newer_than_b("hwe-rolling", "hwe-rolling-lowlatency")
        )


class TestValidateHweKernel(MAASServerTestCase):
    def test_validate_hwe_kernel_returns_default_kernel(self):
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-u")
        hwe_kernel = validate_hwe_kernel(
            None, None, "amd64/generic", "ubuntu", "trusty"
        )
        self.assertEqual(hwe_kernel, "hwe-t")

    def test_validate_hwe_kernel_set_kernel(self):
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-v")
        hwe_kernel = validate_hwe_kernel(
            "hwe-v", None, "amd64/generic", "ubuntu", "trusty"
        )
        self.assertEqual(hwe_kernel, "hwe-v")

    def test_validate_hwe_kernel_accepts_ga_kernel(self):
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("ga-16.04",)
        hwe_kernel = validate_hwe_kernel(
            "ga-16.04", None, "amd64/generic", "ubuntu", "xenial"
        )
        self.assertEqual(hwe_kernel, "ga-16.04")

    def test_validate_hwe_kernel_fails_with_nongeneric_arch_and_kernel(self):
        exception_raised = False
        try:
            validate_hwe_kernel(
                "hwe-v", None, "armfh/hardbank", "ubuntu", "trusty"
            )
        except ValidationError as e:
            self.assertEqual(
                "Subarchitecture(hardbank) must be generic when setting "
                + "hwe_kernel.",
                e.message,
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kernel_fails_with_missing_hwe_kernel(self):
        exception_raised = False
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-u")
        try:
            validate_hwe_kernel(
                "hwe-v", None, "amd64/generic", "ubuntu", "trusty"
            )
        except ValidationError as e:
            self.assertEqual(
                "hwe-v is not available for ubuntu/trusty on amd64/generic.",
                e.message,
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kernel_fails_with_old_kernel_and_newer_release(self):
        exception_raised = False
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-v")
        try:
            validate_hwe_kernel(
                "hwe-t", None, "amd64/generic", "ubuntu", "vivid"
            )
        except ValidationError as e:
            self.assertEqual(
                "hwe-t is too old to use on ubuntu/vivid.", e.message
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kern_fails_with_old_kern_and_new_min_hwe_kern(self):
        exception_raised = False
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-v")
        try:
            validate_hwe_kernel(
                "hwe-t", "hwe-v", "amd64/generic", "ubuntu", "precise"
            )
        except ValidationError as e:
            self.assertEqual(
                "hwe_kernel(hwe-t) is older than min_hwe_kernel(hwe-v).",
                e.message,
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kernel_fails_with_no_avalible_kernels(self):
        exception_raised = False
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-v")
        try:
            validate_hwe_kernel(
                "hwe-t", "hwe-v", "amd64/generic", "ubuntu", "precise"
            )
        except ValidationError as e:
            self.assertEqual(
                "hwe_kernel(hwe-t) is older than min_hwe_kernel(hwe-v).",
                e.message,
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kern_fails_with_old_release_and_newer_hwe_kern(self):
        exception_raised = False
        try:
            validate_hwe_kernel(
                None, "hwe-v", "amd64/generic", "ubuntu", "trusty"
            )
        except ValidationError as e:
            self.assertEqual(
                "trusty has no kernels available which meet"
                + " min_hwe_kernel(hwe-v).",
                e.message,
            )
            exception_raised = True
        self.assertEqual(True, exception_raised)

    def test_validate_hwe_kern_always_sets_kern_with_commissionable_os(self):
        self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        ).return_value = ("hwe-t", "hwe-v")
        mock_get_config = self.patch(Config.objects, "get_config")
        mock_get_config.return_value = "trusty"
        kernel = validate_hwe_kernel(
            None,
            "hwe-v",
            "%s/generic" % factory.make_name("arch"),
            factory.make_name("osystem"),
            factory.make_name("distro"),
        )
        self.assertThat(mock_get_config, MockAnyCall("commissioning_osystem"))
        self.assertThat(
            mock_get_config, MockAnyCall("commissioning_distro_series")
        )
        self.assertEqual("hwe-v", kernel)

    def test_validate_hwe_kern_sets_hwe_kern_to_min_hwe_kern_for_edge(self):
        # Regression test for LP:1654412
        mock_get_usable_hwe_kernels = self.patch(
            BootResource.objects, "get_usable_hwe_kernels"
        )
        mock_get_usable_hwe_kernels.return_value = (
            "hwe-16.04",
            "hwe-16.04-edge",
        )
        arch = factory.make_name("arch")

        kernel = validate_hwe_kernel(
            None, "hwe-16.04-edge", "%s/generic" % arch, "ubuntu", "xenial"
        )

        self.assertEquals("hwe-16.04-edge", kernel)
        self.assertThat(
            mock_get_usable_hwe_kernels,
            MockCalledOnceWith("ubuntu/xenial", arch, "generic"),
        )


class TestValidateMinHweKernel(MAASServerTestCase):
    def test_validates_kernel(self):
        kernel = factory.make_kernel_string(generic_only=True)
        self.patch(
            BootResource.objects, "get_supported_hwe_kernels"
        ).return_value = (kernel,)
        self.assertEquals(kernel, validate_min_hwe_kernel(kernel))

    def test_returns_empty_string_when_none(self):
        self.assertEquals("", validate_min_hwe_kernel(None))

    def test_raises_exception_when_not_found(self):
        self.assertRaises(
            ValidationError,
            validate_min_hwe_kernel,
            factory.make_kernel_string(),
        )

    def test_raises_exception_when_lowlatency(self):
        self.assertRaises(
            ValidationError, validate_min_hwe_kernel, "hwe-16.04-lowlatency"
        )


class TestGetReleaseFromDistroInfo(MAASServerTestCase):
    def pick_release(self):
        ubuntu = UbuntuDistroInfo()
        try:
            ubuntu_rows = ubuntu._rows
        except AttributeError:
            ubuntu_rows = [row.__dict__ for row in ubuntu._releases]
        supported_releases = [
            release
            for release in ubuntu_rows
            if int(release["version"].split(".")[0]) >= 12
        ]
        return random.choice(supported_releases)

    def test_finds_by_series(self):
        release = self.pick_release()
        self.assertEqual(
            release, get_release_from_distro_info(release["series"])
        )

    def test_finds_by_series_first_letter(self):
        release = self.pick_release()
        self.assertEqual(
            release, get_release_from_distro_info(release["series"][0])
        )

    def test_finds_by_version(self):
        release = self.pick_release()
        self.assertEqual(
            release, get_release_from_distro_info(release["version"])
        )

    def test_returns_none_when_not_found(self):
        self.assertIsNone(
            get_release_from_distro_info(factory.make_name("string"))
        )


class TestGetReleaseFromDB(MAASServerTestCase):
    def make_boot_source_cache(self):
        # Disable boot sources signals otherwise the test fails due to unrun
        # post-commit tasks at the end of the test.
        self.useFixture(SignalsDisabled("bootsources"))
        ubuntu = UbuntuDistroInfo()
        try:
            ubuntu_rows = ubuntu._rows
        except AttributeError:
            ubuntu_rows = [row.__dict__ for row in ubuntu._releases]
        supported_releases = [
            release
            for release in ubuntu_rows
            if int(release["version"].split(".")[0]) >= 12
        ]
        release = random.choice(supported_releases)
        ga_or_hwe = random.choice(["hwe", "ga"])
        subarch = "%s-%s" % (ga_or_hwe, release["version"].split(" ")[0])
        factory.make_BootSourceCache(
            os="ubuntu",
            arch=factory.make_name("arch"),
            subarch=subarch,
            release=release["series"],
            release_codename=release["codename"],
            release_title=release["version"],
            support_eol=release.get("eol_server", release.get("eol-server")),
        )
        return release

    def test_finds_by_subarch(self):
        release = self.make_boot_source_cache()
        self.assertEquals(
            release["series"],
            get_release_from_db(release["version"].split(" ")[0])["series"],
        )

    def test_finds_by_release(self):
        release = self.make_boot_source_cache()
        self.assertEquals(
            release["version"],
            get_release_from_db(release["series"])["version"],
        )

    def test_finds_by_release_first_letter(self):
        release = self.make_boot_source_cache()
        self.assertEquals(
            release["version"],
            get_release_from_db(release["series"][0])["version"],
        )

    def test_finds_by_version(self):
        release = self.make_boot_source_cache()
        self.assertItemsEqual(
            release["series"],
            get_release_from_db(release["version"])["series"],
        )

    def test_returns_none_when_not_found(self):
        self.assertIsNone(get_release_from_db(factory.make_name("string")))
