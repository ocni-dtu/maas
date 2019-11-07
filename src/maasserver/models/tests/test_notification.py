# Copyright 2015-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `Notification`."""

__all__ = []

import itertools
import random

from django.core.exceptions import ValidationError
from django.db.models.query import QuerySet
from maasserver.models.notification import Notification
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from testtools.matchers import (
    AfterPreprocessing,
    Equals,
    HasLength,
    Is,
    IsInstance,
    MatchesAll,
    MatchesSetwise,
    MatchesStructure,
    Not,
)


class TestNotificationManagerCreateMethods(MAASServerTestCase):
    """Tests for the `NotificationManager`'s create methods."""

    create_methods = tuple(
        (category, target, "create_%s_for_%s" % (category.lower(), target))
        for category, target in itertools.product(
            ("error", "warning", "success", "info"),
            ("user", "users", "admins"),
        )
    )

    scenarios = tuple(
        (
            method_name,
            {
                "category": category,
                "method_name": method_name,
                "target_name": target_name,
                "targets_user": target_name == "user",
                "targets_users": target_name == "users",
                "targets_admins": target_name in {"users", "admins"},
            },
        )
        for category, target_name, method_name in create_methods
    )

    def makeNotification(self, *, ident=None, context=None):
        method = getattr(Notification.objects, self.method_name)
        message = factory.make_name("message")

        if self.targets_user:
            user = factory.make_User()
            notification = method(message, user, context=context, ident=ident)
        else:
            user = None
            notification = method(message, context=context, ident=ident)

        self.assertThat(
            notification,
            MatchesStructure(
                user=Is(None) if user is None else Equals(user),
                message=Equals(message),
            ),
        )

        return notification

    def assertNotification(self, notification, *, ident):
        self.assertThat(
            notification,
            MatchesStructure(
                users=Is(self.targets_users),
                admins=Is(self.targets_admins),
                user=Not(Is(None)) if self.targets_user else Is(None),
                ident=Is(None) if ident is None else Equals(ident),
                category=Equals(self.category),
            ),
        )

    def test_create_new_notification_without_context(self):
        notification = self.makeNotification()
        self.assertNotification(notification, ident=None)
        self.assertThat(notification.context, Equals({}))

    def test_create_new_notification_with_context(self):
        context = {factory.make_name("key"): factory.make_name("value")}
        notification = self.makeNotification(context=context)
        self.assertNotification(notification, ident=None)
        self.assertThat(notification.context, Equals(context))

    def test_create_new_notification_with_ident(self):
        ident = factory.make_name("ident")
        notification = self.makeNotification(ident=ident)
        self.assertNotification(notification, ident=ident)

    def test_create_new_notification_with_reused_ident(self):
        # A new notification is created, and the ident is moved.
        ident = factory.make_name("ident")
        n1 = self.makeNotification(ident=ident)
        n2 = self.makeNotification(ident=ident)
        n1.refresh_from_db()  # Get current value of `ident`.
        self.assertThat(n2, Not(Equals(n1)))
        self.assertNotification(n1, ident=None)
        self.assertNotification(n2, ident=ident)
        self.assertThat(Notification.objects.filter(ident=ident), HasLength(1))


class TestFindingAndDismissingNotifications(MAASServerTestCase):
    """Tests for finding and dismissing notifications."""

    def notify(self, user):
        message = factory.make_name("message")
        return (
            Notification.objects.create_error_for_user(message, user),
            Notification.objects.create_error_for_users(message),
            Notification.objects.create_error_for_admins(message),
        )

    def assertNotifications(self, user, notifications):
        self.assertThat(
            Notification.objects.find_for_user(user),
            MatchesAll(
                IsInstance(QuerySet),  # Not RawQuerySet.
                MatchesSetwise(*map(Equals, notifications)),
            ),
        )

    def test_find_and_dismiss_notifications_for_user(self):
        user = factory.make_User()
        n_for_user, n_for_users, n_for_admins = self.notify(user)
        self.assertNotifications(user, [n_for_user, n_for_users])
        n_for_user.dismiss(user)
        self.assertNotifications(user, [n_for_users])
        n_for_users.dismiss(user)
        self.assertNotifications(user, [])

    def test_find_and_dismiss_notifications_for_users(self):
        user = factory.make_User("user")
        user2 = factory.make_User("user2")
        n_for_user, n_for_users, n_for_admins = self.notify(user)
        self.assertNotifications(user, [n_for_user, n_for_users])
        self.assertNotifications(user2, [n_for_users])
        n_for_users.dismiss(user2)
        self.assertNotifications(user, [n_for_user, n_for_users])
        self.assertNotifications(user2, [])

    def test_find_and_dismiss_notifications_for_admins(self):
        user = factory.make_User("user")
        admin = factory.make_admin("admin")
        n_for_user, n_for_users, n_for_admins = self.notify(user)
        self.assertNotifications(user, [n_for_user, n_for_users])
        self.assertNotifications(admin, [n_for_users, n_for_admins])
        n_for_users.dismiss(admin)
        self.assertNotifications(user, [n_for_user, n_for_users])
        self.assertNotifications(admin, [n_for_admins])
        n_for_admins.dismiss(admin)
        self.assertNotifications(user, [n_for_user, n_for_users])
        self.assertNotifications(admin, [])


class TestNotification(MAASServerTestCase):
    """Tests for the `Notification`."""

    def test_render_combines_message_with_context(self):
        thing_a = factory.make_name("a")
        thing_b = random.randrange(1000)
        message = "There are {b:d} of {a} in my suitcase."
        context = {"a": thing_a, "b": thing_b}
        notification = Notification(message=message, context=context)
        self.assertThat(
            notification.render(),
            Equals(
                "There are "
                + str(thing_b)
                + " of "
                + thing_a
                + " in my suitcase."
            ),
        )

    def test_render_allows_markup_in_message_but_escapes_context(self):
        message = "<foo>{bar}</foo>"
        context = {"bar": "<BAR>"}
        notification = Notification(message=message, context=context)
        self.assertThat(
            notification.render(), Equals("<foo>&lt;BAR&gt;</foo>")
        )

    def test_save_checks_that_rendering_works(self):
        message = "Dude, where's my {thing}?"
        notification = Notification(message=message)
        error = self.assertRaises(ValidationError, notification.save)
        self.assertThat(
            error.message_dict,
            Equals({"__all__": ["Notification cannot be rendered."]}),
        )
        self.assertThat(notification.id, Is(None))
        self.assertThat(Notification.objects.all(), HasLength(0))

    def test_is_relevant_to_user(self):
        make_Notification = factory.make_Notification

        user = factory.make_User()
        user2 = factory.make_User()
        admin = factory.make_admin()

        Yes, No = Is(True), Is(False)

        def assertRelevance(notification, user, yes_or_no):
            # Ensure that is_relevant_to and find_for_user agree, i.e. if
            # is_relevant_to returns True, the notification is in the set
            # returned by find_for_user. Likewise, if is_relevant_to returns
            # False, the notification is not in the find_for_user set.
            self.assertThat(notification.is_relevant_to(user), yes_or_no)
            self.assertThat(
                Notification.objects.find_for_user(user)
                .filter(id=notification.id)
                .exists(),
                yes_or_no,
            )

        notification_to_user = make_Notification(user=user)
        assertRelevance(notification_to_user, None, No)
        assertRelevance(notification_to_user, user, Yes)
        assertRelevance(notification_to_user, user2, No)
        assertRelevance(notification_to_user, admin, No)

        notification_to_users = make_Notification(users=True)
        assertRelevance(notification_to_users, None, No)
        assertRelevance(notification_to_users, user, Yes)
        assertRelevance(notification_to_users, user2, Yes)
        assertRelevance(notification_to_users, admin, No)

        notification_to_admins = make_Notification(admins=True)
        assertRelevance(notification_to_admins, None, No)
        assertRelevance(notification_to_admins, user, No)
        assertRelevance(notification_to_admins, user2, No)
        assertRelevance(notification_to_admins, admin, Yes)

        notification_to_all = make_Notification(users=True, admins=True)
        assertRelevance(notification_to_all, None, No)
        assertRelevance(notification_to_all, user, Yes)
        assertRelevance(notification_to_all, user2, Yes)
        assertRelevance(notification_to_all, admin, Yes)


class TestNotificationRepresentation(MAASServerTestCase):
    """Tests for the `Notification` representation."""

    scenarios = tuple(
        (category, dict(category=category))
        for category in ("error", "warning", "success", "info")
    )

    def test_for_user(self):
        notification = Notification(
            user=factory.make_User("foobar"),
            message="The cat in the {place}",
            context=dict(place="bear trap"),
            category=self.category,
        )
        self.assertThat(
            notification,
            AfterPreprocessing(
                repr,
                Equals(
                    "<Notification %s user='foobar' users=False admins=False "
                    "'The cat in the bear trap'>" % self.category.upper()
                ),
            ),
        )

    def test_for_users(self):
        notification = Notification(
            users=True,
            message="The cat in the {place}",
            context=dict(place="blender"),
            category=self.category,
        )
        self.assertThat(
            notification,
            AfterPreprocessing(
                repr,
                Equals(
                    "<Notification %s user=None users=True admins=False "
                    "'The cat in the blender'>" % self.category.upper()
                ),
            ),
        )

    def test_for_admins(self):
        notification = Notification(
            admins=True,
            message="The cat in the {place}",
            context=dict(place="lava pit"),
            category=self.category,
        )
        self.assertThat(
            notification,
            AfterPreprocessing(
                repr,
                Equals(
                    "<Notification %s user=None users=False admins=True "
                    "'The cat in the lava pit'>" % self.category.upper()
                ),
            ),
        )
