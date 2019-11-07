# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the `Config` class and friends."""

__all__ = []

from socket import gethostname

from django.db import IntegrityError
from django.http import HttpRequest
from fixtures import TestWithFixtures
from maasserver.enum import ENDPOINT_CHOICES
from maasserver.models import Config, Event, signals
import maasserver.models.config
from maasserver.models.config import get_default_config
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from provisioningserver.events import AUDIT
from testtools.matchers import Is


class ConfigDefaultTest(MAASServerTestCase, TestWithFixtures):
    """Test config default values."""

    def test_default_config_maas_name(self):
        default_config = get_default_config()
        self.assertEqual(gethostname(), default_config["maas_name"])

    def test_defaults(self):
        expected = get_default_config()
        observed = {name: Config.objects.get_config(name) for name in expected}

        # Test isolation is not what it ought to be, so we have to exclude
        # rpc_shared_secret here for now. Attempts to improve isolation have
        # so far resulted in random unreproducible test failures. See the
        # merge proposal for lp:~allenap/maas/increased-test-isolation.
        self.assertIn("rpc_shared_secret", expected)
        del expected["rpc_shared_secret"]
        self.assertIn("rpc_shared_secret", observed)
        del observed["rpc_shared_secret"]

        # completed_intro is set to True in all tests so that URL manipulation
        # in the middleware does not occur. We check that it is True and
        # remove it from the expected and observed.
        self.assertTrue(observed["completed_intro"])
        del expected["completed_intro"]
        del observed["completed_intro"]

        self.assertEqual(expected, observed)


class CallRecorder:
    """A utility class which tracks the calls to its 'call' method and
    stores the arguments given to 'call' in 'self.calls'.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append([args, kwargs])


class ConfigTest(MAASServerTestCase):
    """Testing of the :class:`Config` model and its related manager class."""

    def test_config_name_uniqueness_enforced(self):
        name = factory.make_name("name")
        Config.objects.create(name=name, value=factory.make_name("value"))
        self.assertRaises(
            IntegrityError,
            Config.objects.create,
            name=name,
            value=factory.make_name("value"),
        )

    def test_manager_get_config_found(self):
        Config.objects.create(name="name", value="config")
        config = Config.objects.get_config("name")
        self.assertEqual("config", config)

    def test_manager_get_config_not_found(self):
        config = Config.objects.get_config("name", "default value")
        self.assertEqual("default value", config)

    def test_manager_get_config_not_found_none(self):
        config = Config.objects.get_config("name")
        self.assertIsNone(config)

    def test_manager_get_config_not_found_in_default_config(self):
        name = factory.make_string()
        value = factory.make_string()
        self.patch(maasserver.models.config, "DEFAULT_CONFIG", {name: value})
        config = Config.objects.get_config(name, None)
        self.assertEqual(value, config)

    def test_default_config_cannot_be_changed(self):
        name = factory.make_string()
        self.patch(
            maasserver.models.config,
            "DEFAULT_CONFIG",
            {name: {"key": "value"}},
        )
        config = Config.objects.get_config(name)
        config.update({"key2": "value2"})

        self.assertEqual({"key": "value"}, Config.objects.get_config(name))

    def test_manager_get_configs_returns_configs_dict(self):
        expected = get_default_config()
        # Only get a subset of all the configs.
        expected_names = list(expected)[:5]
        # Set a config value to test that is over the default.
        other_value = factory.make_name("value")
        Config.objects.set_config(expected_names[0], other_value)
        observed = Config.objects.get_configs(expected_names)
        expected_dict = {expected_names[0]: other_value}
        expected_dict.update(
            {
                name: expected[name]
                for name in expected_names
                if name != expected_names[0]
            }
        )
        self.assertEquals(expected_dict, observed)

    def test_manager_get_configs_returns_passed_defaults(self):
        expected = get_default_config()
        # Only get a subset of all the configs.
        expected_names = list(expected)[:5]
        expected_dict = {
            name: factory.make_name("value") for name in expected_names
        }
        defaults = [expected_dict[name] for name in expected_names]
        for name, value in expected_dict.items():
            Config.objects.set_config(name, value)
        self.assertEquals(
            expected_dict, Config.objects.get_configs(expected_names, defaults)
        )

    def test_manager_set_config_creates_config(self):
        Config.objects.set_config("name", "config1")
        Config.objects.set_config("name", "config2")
        self.assertSequenceEqual(
            ["config2"],
            [config.value for config in Config.objects.filter(name="name")],
        )

    def test_manager_set_config_creates_audit_event(self):
        user = factory.make_User()
        request = HttpRequest()
        request.user = user
        endpoint = factory.pick_choice(ENDPOINT_CHOICES)
        Config.objects.set_config("name", "value", endpoint, request)
        event = Event.objects.get(type__level=AUDIT)
        self.assertIsNotNone(event)
        self.assertEqual(
            event.description,
            "Updated configuration setting 'name' to 'value'.",
        )

    def test_manager_config_changed_connect_connects(self):
        recorder = CallRecorder()
        name = factory.make_string()
        value = factory.make_string()
        Config.objects.config_changed_connect(name, recorder)
        Config.objects.set_config(name, value)
        config = Config.objects.get(name=name)

        self.assertEqual(1, len(recorder.calls))
        self.assertEqual((Config, config, True), recorder.calls[0][0])

    def test_manager_config_changed_connect_connects_multiple(self):
        recorder = CallRecorder()
        recorder2 = CallRecorder()
        name = factory.make_string()
        value = factory.make_string()
        Config.objects.config_changed_connect(name, recorder)
        Config.objects.config_changed_connect(name, recorder2)
        Config.objects.set_config(name, value)

        self.assertEqual(1, len(recorder.calls))
        self.assertEqual(1, len(recorder2.calls))

    def test_manager_config_changed_connect_connects_multiple_same(self):
        # If the same method is connected twice, it will only get called
        # once.
        recorder = CallRecorder()
        name = factory.make_string()
        value = factory.make_string()
        Config.objects.config_changed_connect(name, recorder)
        Config.objects.config_changed_connect(name, recorder)
        Config.objects.set_config(name, value)

        self.assertEqual(1, len(recorder.calls))

    def test_manager_config_changed_connect_connects_by_config_name(self):
        recorder = CallRecorder()
        name = factory.make_string()
        value = factory.make_string()
        Config.objects.config_changed_connect(name, recorder)
        another_name = factory.make_string()
        Config.objects.set_config(another_name, value)

        self.assertEqual(0, len(recorder.calls))

    def test_manager_config_changed_disconnect_disconnects(self):
        recorder = CallRecorder()
        name = factory.make_string()
        value = factory.make_string()
        Config.objects.config_changed_connect(name, recorder)
        Config.objects.config_changed_disconnect(name, recorder)
        Config.objects.set_config(name, value)

        self.assertEqual([], recorder.calls)

    def test_manager_is_external_auth_enabled_false(self):
        self.assertFalse(Config.objects.is_external_auth_enabled())

    def test_manager_is_external_auth_enabled_true(self):
        Config.objects.set_config(
            "external_auth_url", "http://auth.example.com"
        )
        self.assertTrue(Config.objects.is_external_auth_enabled())


class SettingConfigTest(MAASServerTestCase):
    """Testing of the :class:`Config` model and setting each option."""

    scenarios = tuple((name, {"name": name}) for name in get_default_config())

    def setUp(self):
        super(SettingConfigTest, self).setUp()
        # Some of these setting we have to be careful about.
        if self.name in {"enable_http_proxy", "http_proxy"}:
            manager = signals.bootsources.signals
            self.addCleanup(manager.enable)
            manager.disable()

    def test_can_be_initialised_to_None_without_crashing(self):
        Config.objects.set_config(self.name, None)
        self.assertThat(Config.objects.get_config(self.name), Is(None))

    def test_can_be_modified_from_None_without_crashing(self):
        Config.objects.set_config(self.name, None)
        something = [factory.make_name("value")]
        Config.objects.set_config(self.name, something)
        self.assertEqual(something, Config.objects.get_config(self.name))
