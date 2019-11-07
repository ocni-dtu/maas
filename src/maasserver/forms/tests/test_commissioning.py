# Copyright 2014-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for commissioning forms."""

__all__ = []

from maasserver.enum import BOOT_RESOURCE_TYPE
from maasserver.forms import CommissioningForm
from maasserver.models import Config
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.forms import compose_invalid_choice_text


class TestCommissioningFormForm(MAASServerTestCase):
    def test_commissioningform_error_msg_lists_series_choices(self):
        form = CommissioningForm()
        field = form.fields["commissioning_distro_series"]
        self.assertEqual(
            compose_invalid_choice_text(
                "commissioning_distro_series", field.choices
            ),
            field.error_messages["invalid_choice"],
        )

    def test_commissioningform_error_msg_lists_min_hwe_kernel_choices(self):
        form = CommissioningForm()
        field = form.fields["default_min_hwe_kernel"]
        self.assertEqual(
            compose_invalid_choice_text(
                "default_min_hwe_kernel", field.choices
            ),
            field.error_messages["invalid_choice"],
        )

    def test_commissioningform_contains_real_and_ui_choice(self):
        release = factory.pick_ubuntu_release()
        name = "ubuntu/%s" % release
        arch = factory.make_name("arch")
        kernel = "hwe-" + release[0]
        # Disable boot sources signals otherwise the test fails due to unrun
        # post-commit tasks at the end of the test.
        self.useFixture(SignalsDisabled("bootsources"))
        factory.make_BootSourceCache(os=name, subarch=kernel, release=release)
        factory.make_usable_boot_resource(
            name=name,
            architecture="%s/%s" % (arch, kernel),
            rtype=BOOT_RESOURCE_TYPE.SYNCED,
        )
        Config.objects.set_config("commissioning_distro_series", release)
        form = CommissioningForm()
        self.assertItemsEqual(
            [
                ("", "--- No minimum kernel ---"),
                (kernel, "%s (%s)" % (release, kernel)),
            ],
            form.fields["default_min_hwe_kernel"].choices,
        )
