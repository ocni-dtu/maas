# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `MachineWithMACAddressesForm`."""

__all__ = []

from django.http import QueryDict
from maasserver.enum import INTERFACE_TYPE, NODE_STATUS
from maasserver.forms import MachineWithMACAddressesForm
from maasserver.testing.architecture import (
    make_usable_architecture,
    patch_usable_architectures,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from testtools.matchers import Contains


class MachineWithMACAddressesFormTest(MAASServerTestCase):
    def get_QueryDict(self, params):
        query_dict = QueryDict("", mutable=True)
        for k, v in params.items():
            if isinstance(v, list):
                query_dict.setlist(k, v)
            else:
                query_dict[k] = v
        return query_dict

    def make_params(
        self, mac_addresses=None, architecture=None, hostname=None
    ):
        if mac_addresses is None:
            mac_addresses = [factory.make_mac_address()]
        if architecture is None:
            architecture = factory.make_name("arch")
        if hostname is None:
            hostname = factory.make_name("hostname")
        params = {
            "mac_addresses": mac_addresses,
            "architecture": architecture,
            "hostname": hostname,
        }
        # Make sure that the architecture parameter is acceptable.
        patch_usable_architectures(self, [architecture])
        return self.get_QueryDict(params)

    def test__valid(self):
        architecture = make_usable_architecture(self)
        form = MachineWithMACAddressesForm(
            data=self.make_params(
                mac_addresses=["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
                architecture=architecture,
            )
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            ["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
            form.cleaned_data["mac_addresses"],
        )
        self.assertEqual(architecture, form.cleaned_data["architecture"])

    def test__simple_invalid(self):
        # If the form only has one (invalid) MAC address field to validate,
        # the error message in form.errors['mac_addresses'] is the
        # message from the field's validation error.
        form = MachineWithMACAddressesForm(
            data=self.make_params(mac_addresses=["invalid"])
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(["mac_addresses"], list(form.errors))
        self.assertEqual(
            ["'invalid' is not a valid MAC address."],
            form.errors["mac_addresses"],
        )

    def test__multiple_invalid(self):
        # If the form has multiple MAC address fields to validate,
        # if one or more fields are invalid, a single error message is
        # present in form.errors['mac_addresses'] after validation.
        form = MachineWithMACAddressesForm(
            data=self.make_params(mac_addresses=["invalid_1", "invalid_2"])
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(["mac_addresses"], list(form.errors))
        self.assertEqual(
            [
                "One or more MAC addresses is invalid. "
                "('invalid_1' is not a valid MAC address. \u2014"
                " 'invalid_2' is not a valid MAC address.)"
            ],
            form.errors["mac_addresses"],
        )

    def test__mac_in_use_on_current_node_passes(self):
        node = factory.make_Node_with_Interface_on_Subnet(
            address="aa:bb:cc:dd:ee:ff"
        )
        architecture = make_usable_architecture(self)
        form = MachineWithMACAddressesForm(
            data=self.make_params(
                mac_addresses=["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
                architecture=architecture,
            ),
            instance=node,
        )

        self.assertTrue(form.is_valid(), dict(form.errors))
        self.assertEqual(
            ["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
            form.cleaned_data["mac_addresses"],
        )
        self.assertEqual(architecture, form.cleaned_data["architecture"])

    def test__with_mac_in_use_on_another_node_fails(self):
        factory.make_Node_with_Interface_on_Subnet(address="aa:bb:cc:dd:ee:ff")
        architecture = make_usable_architecture(self)
        node = factory.make_Node_with_Interface_on_Subnet()
        form = MachineWithMACAddressesForm(
            data=self.make_params(
                mac_addresses=["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
                architecture=architecture,
            ),
            instance=node,
        )

        self.assertFalse(form.is_valid(), dict(form.errors))
        self.assertThat(dict(form.errors), Contains("mac_addresses"))

    def test__with_mac_in_use_on_uknown_interface_passes(self):
        factory.make_Interface(
            INTERFACE_TYPE.UNKNOWN, mac_address="aa:bb:cc:dd:ee:ff"
        )
        architecture = make_usable_architecture(self)
        form = MachineWithMACAddressesForm(
            data=self.make_params(
                mac_addresses=["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
                architecture=architecture,
            )
        )

        self.assertTrue(form.is_valid(), dict(form.errors))
        self.assertEqual(
            ["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"],
            form.cleaned_data["mac_addresses"],
        )
        self.assertEqual(architecture, form.cleaned_data["architecture"])

    def test__empty(self):
        # Empty values in the list of MAC addresses are simply ignored.
        form = MachineWithMACAddressesForm(
            data=self.make_params(
                mac_addresses=[factory.make_mac_address(), ""]
            )
        )

        self.assertTrue(form.is_valid())

    def test__mac_address_is_required(self):
        form = MachineWithMACAddressesForm(
            data=self.make_params(mac_addresses=[])
        )

        self.assertFalse(form.is_valid())
        self.assertEqual(["mac_addresses"], list(form.errors))
        self.assertEqual(
            ["This field is required."], form.errors["mac_addresses"]
        )

    def test__no_architecture_or_mac_addresses_is_ok_for_ipmi(self):
        # No architecture or MAC addresses is okay for IPMI power types.
        params = self.make_params(mac_addresses=[])
        params["architecture"] = None
        params["power_type"] = "ipmi"
        form = MachineWithMACAddressesForm(data=params)
        self.assertTrue(form.is_valid())

    def test__save(self):
        macs = ["aa:bb:cc:dd:ee:ff", "9a:bb:c3:33:e5:7f"]
        form = MachineWithMACAddressesForm(
            data=self.make_params(mac_addresses=macs)
        )
        self.assertTrue(form.is_valid())
        node = form.save()

        self.assertIsNotNone(node.id)  # The node is persisted.
        self.assertEquals(NODE_STATUS.NEW, node.status)
        self.assertItemsEqual(
            macs, [nic.mac_address for nic in node.interface_set.all()]
        )

    def test_form_without_hostname_generates_hostname(self):
        form = MachineWithMACAddressesForm(data=self.make_params(hostname=""))
        self.assertTrue(form.is_valid())
        node = form.save()
        self.assertTrue(len(node.hostname) > 0)

    def test_form_with_ip_based_hostname_generates_hostname(self):
        ip_based_hostname = "192-168-12-10.maas"
        form = MachineWithMACAddressesForm(
            data=self.make_params(hostname=ip_based_hostname)
        )
        self.assertTrue(form.is_valid())
        node = form.save()
        self.assertNotEqual("192-168-12-10", node.hostname)

    def test_form_with_ip_based_hostname_prefix_valid(self):
        ip_prefixed_hostname = "192-168-12-10-extra.maas"
        form = MachineWithMACAddressesForm(
            data=self.make_params(hostname=ip_prefixed_hostname)
        )
        self.assertTrue(form.is_valid())
        node = form.save()
        self.assertEqual("192-168-12-10-extra", node.hostname)

    def test_form_with_commissioning(self):
        form = MachineWithMACAddressesForm(
            data={"commission": True, **self.make_params()}
        )
        self.assertTrue(form.is_valid())
        machine = form.save()
        self.assertEquals(NODE_STATUS.COMMISSIONING, machine.status)
        self.assertIsNotNone(machine.current_commissioning_script_set)
