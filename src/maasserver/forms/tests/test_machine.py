# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for node forms."""

__all__ = []

from crochet import TimeoutError
from maasserver import forms
from maasserver.clusterrpc.driver_parameters import get_driver_choices
from maasserver.forms import (
    AdminMachineForm,
    BLANK_CHOICE,
    MachineForm,
    pick_default_architecture,
)
from maasserver.testing.architecture import (
    make_usable_architecture,
    patch_usable_architectures,
)
from maasserver.testing.factory import factory
from maasserver.testing.osystems import (
    make_osystem_with_releases,
    make_usable_osystem,
    patch_usable_osystems,
)
from maasserver.testing.testcase import MAASServerTestCase
from provisioningserver.rpc.exceptions import (
    NoConnectionsAvailable,
    NoSuchOperatingSystem,
)
from provisioningserver.testing.os import make_osystem


class TestMachineForm(MAASServerTestCase):
    def test_contains_limited_set_of_fields(self):
        form = MachineForm()

        self.assertItemsEqual(
            [
                "hostname",
                "domain",
                "architecture",
                "osystem",
                "distro_series",
                "license_key",
                "disable_ipv4",
                "swap_size",
                "min_hwe_kernel",
                "hwe_kernel",
                "install_rackd",
                "ephemeral_deploy",
                "commission",
            ],
            list(form.fields),
        )

    def test_accepts_usable_architecture(self):
        arch = make_usable_architecture(self)
        form = MachineForm(
            data={"hostname": factory.make_name("host"), "architecture": arch}
        )
        self.assertTrue(form.is_valid(), form._errors)

    def test_rejects_unusable_architecture(self):
        patch_usable_architectures(self)
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": factory.make_name("arch"),
            }
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["architecture"], form._errors.keys())

    def test_starts_with_default_architecture(self):
        arches = sorted([factory.make_name("arch") for _ in range(5)])
        patch_usable_architectures(self, arches)
        form = MachineForm()
        self.assertEqual(
            pick_default_architecture(arches),
            form.fields["architecture"].initial,
        )

    def test_form_validates_hwe_kernel_by_passing_invalid_config(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = make_usable_osystem(self)
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem["name"],
                "min_hwe_kernel": "hwe-t",
                "hwe_kernel": "hwe-p",
            },
            instance=node,
        )
        self.assertEqual(form.is_valid(), False)

    def test_form_validates_min_hwe_kernel_by_passing_invalid_config(self):
        node = factory.make_Node(min_hwe_kernel="hwe-t")
        form = MachineForm(instance=node)
        self.assertEqual(form.is_valid(), False)

    def test_adds_blank_default_when_no_arches_available(self):
        patch_usable_architectures(self, [])
        form = MachineForm()
        self.assertEqual([BLANK_CHOICE], form.fields["architecture"].choices)

    def test_accepts_osystem(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = make_usable_osystem(self)
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem["name"],
            },
            instance=node,
        )
        self.assertTrue(form.is_valid(), form._errors)

    def test_rejects_invalid_osystem(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        patch_usable_osystems(self)
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": factory.make_name("os"),
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["osystem"], form._errors.keys())

    def test_starts_with_default_osystem(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystems = [make_osystem_with_releases(self) for _ in range(5)]
        patch_usable_osystems(self, osystems)
        form = MachineForm(instance=node)
        self.assertEqual("", form.fields["osystem"].initial)

    def test_accepts_osystem_distro_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = make_usable_osystem(self)
        release = osystem["default_release"]
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem["name"],
                "distro_series": "%s/%s" % (osystem["name"], release),
            },
            instance=node,
        )
        self.assertTrue(form.is_valid(), form._errors)

    def test_rejects_invalid_osystem_distro_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = make_usable_osystem(self)
        release = factory.make_name("release")
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem["name"],
                "distro_series": "%s/%s" % (osystem["name"], release),
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["distro_series"], form._errors.keys())

    def test_set_distro_series_accepts_short_distro_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        release = factory.make_name("release")
        make_usable_osystem(
            self, releases=[release + "6", release + "0", release + "3"]
        )
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
            },
            instance=node,
        )
        form.set_distro_series(release)
        form.save()
        self.assertEqual(release + "6", node.distro_series)

    def test_set_distro_series_doesnt_allow_short_ubuntu_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        make_usable_osystem(self, osystem_name="ubuntu", releases=["trusty"])
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
            },
            instance=node,
        )
        form.set_distro_series("trust")
        self.assertFalse(form.is_valid())

    def test_starts_with_default_distro_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystems = [make_osystem_with_releases(self) for _ in range(5)]
        patch_usable_osystems(self, osystems)
        form = MachineForm(instance=node)
        self.assertEqual("", form.fields["distro_series"].initial)

    def test_rejects_mismatch_osystem_distro_series(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = make_usable_osystem(self)
        release = osystem["default_release"]
        invalid = factory.make_name("invalid_os")
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem["name"],
                "distro_series": "%s/%s" % (invalid, release),
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["distro_series"], form._errors.keys())

    def test_rejects_when_validate_license_key_returns_False(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = factory.make_name("osystem")
        release = factory.make_name("release")
        distro_series = "%s/%s" % (osystem, release)
        make_osystem(self, osystem, [release])
        factory.make_BootResource(name=distro_series)
        license_key = factory.make_name("key")
        mock_validate = self.patch(forms, "validate_license_key")
        mock_validate.return_value = False
        form = MachineForm(
            data={
                "hostname": factory.make_name("host"),
                "architecture": make_usable_architecture(self),
                "osystem": osystem,
                "distro_series": distro_series,
                "license_key": license_key,
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["license_key"], form._errors.keys())

    def test_rejects_when_validate_license_key_for_returns_False(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = factory.make_name("osystem")
        release = factory.make_name("release")
        distro_series = "%s/%s" % (osystem, release)
        make_osystem(self, osystem, [release])
        factory.make_BootResource(name=distro_series)
        license_key = factory.make_name("key")
        mock_validate_for = self.patch(forms, "validate_license_key_for")
        mock_validate_for.return_value = False
        form = MachineForm(
            data={
                "architecture": make_usable_architecture(self),
                "osystem": osystem,
                "distro_series": distro_series,
                "license_key": license_key,
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["license_key"], form._errors.keys())

    def test_rejects_when_validate_license_key_for_raise_no_connection(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = factory.make_name("osystem")
        release = factory.make_name("release")
        distro_series = "%s/%s" % (osystem, release)
        make_osystem(self, osystem, [release])
        factory.make_BootResource(name=distro_series)
        license_key = factory.make_name("key")
        mock_validate_for = self.patch(forms, "validate_license_key_for")
        mock_validate_for.side_effect = NoConnectionsAvailable()
        form = MachineForm(
            data={
                "architecture": make_usable_architecture(self),
                "osystem": osystem,
                "distro_series": distro_series,
                "license_key": license_key,
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["license_key"], form._errors.keys())

    def test_rejects_when_validate_license_key_for_raise_timeout(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = factory.make_name("osystem")
        release = factory.make_name("release")
        distro_series = "%s/%s" % (osystem, release)
        make_osystem(self, osystem, [release])
        factory.make_BootResource(name=distro_series)
        license_key = factory.make_name("key")
        mock_validate_for = self.patch(forms, "validate_license_key_for")
        mock_validate_for.side_effect = TimeoutError()
        form = MachineForm(
            data={
                "architecture": make_usable_architecture(self),
                "osystem": osystem,
                "distro_series": distro_series,
                "license_key": license_key,
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["license_key"], form._errors.keys())

    def test_rejects_when_validate_license_key_for_raise_no_os(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        osystem = factory.make_name("osystem")
        release = factory.make_name("release")
        distro_series = "%s/%s" % (osystem, release)
        make_osystem(self, osystem, [release])
        factory.make_BootResource(name=distro_series)
        license_key = factory.make_name("key")
        mock_validate_for = self.patch(forms, "validate_license_key_for")
        mock_validate_for.side_effect = NoSuchOperatingSystem()
        form = MachineForm(
            data={
                "architecture": make_usable_architecture(self),
                "osystem": osystem,
                "distro_series": distro_series,
                "license_key": license_key,
            },
            instance=node,
        )
        self.assertFalse(form.is_valid())
        self.assertItemsEqual(["license_key"], form._errors.keys())


class TestAdminMachineForm(MAASServerTestCase):
    def test_AdminMachineForm_contains_limited_set_of_fields(self):
        user = factory.make_User()
        self.client.login(user=user)
        node = factory.make_Node(owner=user)
        form = AdminMachineForm(instance=node)

        self.assertItemsEqual(
            [
                "hostname",
                "description",
                "domain",
                "architecture",
                "osystem",
                "distro_series",
                "license_key",
                "disable_ipv4",
                "swap_size",
                "min_hwe_kernel",
                "hwe_kernel",
                "install_rackd",
                "ephemeral_deploy",
                "cpu_count",
                "memory",
                "zone",
                "power_parameters",
                "power_type",
                "pool",
                "commission",
            ],
            list(form.fields),
        )

    def test_AdminMachineForm_populates_power_type_choices(self):
        form = AdminMachineForm()
        self.assertEqual(
            [""] + [choice[0] for choice in get_driver_choices()],
            [choice[0] for choice in form.fields["power_type"].choices],
        )

    def test_AdminMachineForm_populates_power_type_initial(self):
        node = factory.make_Node()
        form = AdminMachineForm(instance=node)
        self.assertEqual(node.power_type, form.fields["power_type"].initial)

    def test_AdminMachineForm_changes_power_parameters_with_skip_check(self):
        node = factory.make_Node()
        hostname = factory.make_string()
        power_type = factory.pick_power_type()
        power_parameters_field = factory.make_string()
        arch = make_usable_architecture(self)
        form = AdminMachineForm(
            data={
                "hostname": hostname,
                "architecture": arch,
                "power_type": power_type,
                "power_parameters_field": power_parameters_field,
                "power_parameters_skip_check": "true",
            },
            instance=node,
        )
        form.save()

        self.assertEqual(
            (hostname, power_type, {"field": power_parameters_field}),
            (node.hostname, node.power_type, node.power_parameters),
        )

    def test_AdminMachineForm_doesnt_changes_power_parameters(self):
        power_parameters = {"test": factory.make_name("test")}
        node = factory.make_Node(power_parameters=power_parameters)
        hostname = factory.make_string()
        arch = make_usable_architecture(self)
        form = AdminMachineForm(
            data={
                "hostname": hostname,
                "architecture": arch,
                "power_parameters_skip_check": "true",
            },
            instance=node,
        )
        node = form.save()
        self.assertEqual(power_parameters, node.power_parameters)

    def test_AdminMachineForm_doesnt_change_power_type(self):
        power_type = factory.pick_power_type()
        node = factory.make_Node(power_type=power_type)
        hostname = factory.make_string()
        arch = make_usable_architecture(self)
        form = AdminMachineForm(
            data={
                "hostname": hostname,
                "architecture": arch,
                "power_parameters_skip_check": "true",
            },
            instance=node,
        )
        node = form.save()
        self.assertEqual(power_type, node.power_type)

    def test_AdminMachineForm_changes_power_type(self):
        node = factory.make_Node()
        hostname = factory.make_string()
        power_type = factory.pick_power_type()
        arch = make_usable_architecture(self)
        form = AdminMachineForm(
            data={
                "hostname": hostname,
                "architecture": arch,
                "power_type": power_type,
                "power_parameters_skip_check": "true",
            },
            instance=node,
        )
        node = form.save()
        self.assertEqual(power_type, node.power_type)
