# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test maasserver ControllerInfo model."""

__all__ = []

from crochet import wait_for
from maasserver.models import ControllerInfo, Notification
from maasserver.models.controllerinfo import (
    ControllerVersionInfo,
    create_or_update_version_notification,
    KNOWN_VERSION_MISMATCH_NOTIFICATION,
    UNKNOWN_VERSION_MISMATCH_NOTIFICATION,
    VERSION_NOTIFICATION_IDENT,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from testtools.matchers import Equals, Is


wait_for_reactor = wait_for(30)  # 30 seconds.


class TestControllerInfo(MAASServerTestCase):
    def test_str(self):
        controller = factory.make_RackController(hostname="foobar")
        info, _ = ControllerInfo.objects.update_or_create(node=controller)
        self.assertEqual("ControllerInfo (foobar)", str(info))

    def test_controllerinfo_set_version(self):
        controller = factory.make_RackController()
        ControllerInfo.objects.set_version(controller, "2.3.0")
        self.assertThat(controller.version, Equals("2.3.0"))

    def test_controllerinfo_set_infterface_update_info(self):
        controller = factory.make_RackController()
        interfaces = {"eth0": {}}
        hints = ["a", "b", "c"]
        ControllerInfo.objects.set_interface_update_info(
            controller, interfaces, hints
        )
        self.assertThat(controller.interfaces, Equals(interfaces))
        self.assertThat(controller.interface_update_hints, Equals(hints))


class TestGetControllerVersionInfo(MAASServerTestCase):
    def test__sorts_controllerversioninfo_by_most_recent_version_first(self):
        c1 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c1, "2.3.0")
        c2 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c2, "2.4.0")
        c3 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c3, "2.3.5")
        version_info = ControllerInfo.objects.get_controller_version_info()
        # Should have returend a list of ControllerVersionInfo objects.
        for i in range(len(version_info)):
            self.assertThat(
                isinstance(version_info[i], ControllerVersionInfo), Is(True)
            )
        # The versions should be in descending order.
        self.assertThat(version_info[0].version, Equals("2.4.0"))
        self.assertThat(version_info[1].version, Equals("2.3.5"))
        self.assertThat(version_info[2].version, Equals("2.3.0"))


class TestCreateOrUpdateVersionNotification(MAASServerTestCase):
    def test__create(self):
        system_id = "xyzzy"
        create_or_update_version_notification(
            system_id,
            "Fix your MAAS, you slacker! It's only version {ver}.",
            context=dict(ver="1.9"),
        )
        expected_notification = Notification.objects.filter(
            ident=VERSION_NOTIFICATION_IDENT + system_id
        ).first()
        self.assertThat(
            expected_notification.render(),
            Equals("Fix your MAAS, you slacker! It's only version 1.9."),
        )

    def test__update(self):
        system_id = "xyzzy"
        create_or_update_version_notification(
            system_id,
            "Fix your MAAS, you slacker! It's only version {ver}.",
            context=dict(ver="1.9"),
        )
        # The second time should update.
        create_or_update_version_notification(
            system_id,
            "I can't believe you. Still using MAAS {ver}?!",
            context=dict(ver="2.0"),
        )
        expected_notification = Notification.objects.filter(
            ident=VERSION_NOTIFICATION_IDENT + system_id
        ).first()
        self.assertThat(
            expected_notification.render(),
            Equals("I can't believe you. Still using MAAS 2.0?!"),
        )


def get_version_notifications():
    return Notification.objects.filter(
        ident__startswith=VERSION_NOTIFICATION_IDENT
    )


class TestUpdateVersionNotifications(MAASServerTestCase):
    def test__single_controller_never_generates_notifications(self):
        c1 = factory.make_RegionRackController()
        self.assertThat(get_version_notifications().count(), Equals(0))
        ControllerInfo.objects.set_version(c1, "2.3.0")
        self.assertThat(get_version_notifications().count(), Equals(0))
        ControllerInfo.objects.set_version(c1, None)
        self.assertThat(get_version_notifications().count(), Equals(0))
        ControllerInfo.objects.set_version(c1, "")
        self.assertThat(get_version_notifications().count(), Equals(0))

    def test__out_of_date_controller_generates_concise_notification(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c1, "2.3.0-500-g1")
        ControllerInfo.objects.set_version(c2, "2.3.1-500-g1")
        self.assertThat(get_version_notifications().count(), Equals(1))
        self.assertThat(
            get_version_notifications().first().render(),
            Equals(
                KNOWN_VERSION_MISMATCH_NOTIFICATION.format(
                    system_id=c1.system_id, hostname=c1.hostname, v1="2.3.0"
                )
            ),
        )

    def test__version_qualifiers_considered(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionRackController()
        # Note: the revno and git revision are intentionally identical here,
        # so we know they don't affect the comparison of qualifiers, and we
        # know that useless information won't appear in the notification.
        ControllerInfo.objects.set_version(c1, "2.3.0~alpha1-500-g1")
        ControllerInfo.objects.set_version(c2, "2.3.0~alpha2-500-g1")
        self.assertThat(get_version_notifications().count(), Equals(1))
        self.assertThat(
            get_version_notifications().first().render(),
            Equals(
                KNOWN_VERSION_MISMATCH_NOTIFICATION.format(
                    system_id=c1.system_id,
                    hostname=c1.hostname,
                    v1="2.3.0~alpha1",
                )
            ),
        )

    def test__assumes_old_controller_if_version_unknown(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c1, "2.3.0")
        self.assertThat(get_version_notifications().count(), Equals(1))
        self.assertThat(
            get_version_notifications().first().render(),
            Equals(
                UNKNOWN_VERSION_MISMATCH_NOTIFICATION.format(
                    system_id=c2.system_id, hostname=c2.hostname
                )
            ),
        )

    def test__revno_differences_cause_full_version_to_be_shown(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionRackController()
        ControllerInfo.objects.set_version(c1, "2.3.0~beta2-6000-g123abc")
        ControllerInfo.objects.set_version(c2, "2.3.0~beta2-6001-g234bcd")
        self.assertThat(get_version_notifications().count(), Equals(1))
        self.assertThat(
            get_version_notifications().first().render(),
            Equals(
                KNOWN_VERSION_MISMATCH_NOTIFICATION.format(
                    system_id=c1.system_id,
                    hostname=c1.hostname,
                    v1="2.3.0~beta2 (6000-g123abc)",
                )
            ),
        )

    def test__upgrading_controller_causes_old_notifications_to_go_away(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionController()
        ControllerInfo.objects.set_version(c1, "2.3.0~beta2-6000-g123abc")
        ControllerInfo.objects.set_version(c2, "2.3.0~beta2-6001-g234bcd")
        self.assertThat(get_version_notifications().count(), Equals(1))
        ControllerInfo.objects.set_version(c1, "2.3.0~beta2-6001-g234bcd")
        self.assertThat(get_version_notifications().count(), Equals(0))

    def test__deleting_controller_causes_old_notifications_to_go_away(self):
        c1 = factory.make_RegionRackController()
        c2 = factory.make_RegionController()
        ControllerInfo.objects.set_version(c1, "2.3.0~beta2-6000-g123abc")
        ControllerInfo.objects.set_version(c2, "2.3.0~beta2-6001-g234bcd")
        self.assertThat(get_version_notifications().count(), Equals(1))
        c2.delete()
        self.assertThat(get_version_notifications().count(), Equals(0))
