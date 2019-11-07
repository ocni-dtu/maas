# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for register command code."""

__all__ = []

from argparse import ArgumentParser
import io
from itertools import combinations
import pprint
from unittest.mock import call, Mock

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith, MockCallsMatch
from maastesting.testcase import MAASTestCase
from provisioningserver import register_command
from provisioningserver.config import ClusterConfiguration
from provisioningserver.security import (
    get_shared_secret_from_filesystem,
    set_shared_secret_on_filesystem,
    to_hex,
)
from provisioningserver.testing.config import ClusterConfigurationFixture
from provisioningserver.utils.env import get_maas_id
from provisioningserver.utils.shell import ExternalProcessError
from provisioningserver.utils.testing import MAASIDFixture
from testtools.matchers import Equals
from testtools.testcase import ExpectedException


class TestAddArguments(MAASTestCase):
    def test_accepts_all_args(self):
        all_test_arguments = register_command.all_arguments

        default_arg_values = {"--url": None, "--secret": None}

        failures = {}

        # Try all cardinalities of combinations of arguments
        for r in range(len(all_test_arguments) + 1):
            for test_arg_names in combinations(all_test_arguments, r):
                test_values = {
                    "--url": factory.make_simple_http_url(),
                    "--secret": factory.make_name("secret"),
                }

                # Build a query dictionary for the given combination of args
                args_under_test = []
                for param_name in test_arg_names:
                    args_under_test.append(param_name)
                    args_under_test.append(test_values[param_name])

                parser = ArgumentParser()
                register_command.add_arguments(parser)

                observed_args = vars(parser.parse_args(args_under_test))

                expected_args = {}
                for param_name in all_test_arguments:
                    parsed_param_name = param_name[2:].replace("-", "_")

                    if param_name not in test_arg_names:
                        expected_args[parsed_param_name] = default_arg_values[
                            param_name
                        ]
                    else:
                        expected_args[parsed_param_name] = observed_args[
                            parsed_param_name
                        ]

                if expected_args != observed_args:
                    failures[str(test_arg_names)] = {
                        "expected_args": expected_args,
                        "observed_args": observed_args,
                    }

        error_message = io.StringIO()
        error_message.write(
            "One or more key / value argument list(s) passed in the query "
            "string (expected_args) to the API do not match the values in "
            "the returned query string. This means that some arguments were "
            "dropped / added / changed by the the function, which is "
            "incorrect behavior. The list of incorrect arguments is as "
            "follows: \n"
        )
        pp = pprint.PrettyPrinter(depth=3, stream=error_message)
        pp.pprint(failures)
        self.assertDictEqual({}, failures, error_message.getvalue())


class TestRegisterMAASRack(MAASTestCase):
    def setUp(self):
        super(TestRegisterMAASRack, self).setUp()
        self.useFixture(ClusterConfigurationFixture())
        self.mock_call_and_check = self.patch_autospec(
            register_command, "call_and_check"
        )

    def make_args(self, **kwargs):
        args = Mock()
        args.__dict__.update(kwargs)
        return args

    def test____sets_url(self):
        secret = factory.make_bytes()
        expected_url = factory.make_simple_http_url()
        register_command.run(
            self.make_args(url=expected_url, secret=to_hex(secret))
        )
        with ClusterConfiguration.open() as config:
            observed = config.maas_url
        self.assertEqual([expected_url], observed)

    def test___prompts_user_for_url(self):
        expected_url = factory.make_simple_http_url()
        secret = factory.make_bytes()

        stdin = self.patch(register_command, "stdin")
        stdin.isatty.return_value = True

        input = self.patch(register_command, "input")
        input.return_value = expected_url

        register_command.run(self.make_args(url=None, secret=to_hex(secret)))
        with ClusterConfiguration.open() as config:
            observed = config.maas_url

        self.expectThat(
            input, MockCalledOnceWith("MAAS region controller URL: ")
        )
        self.expectThat([expected_url], Equals(observed))

    def test___sets_secret(self):
        url = factory.make_simple_http_url()
        expected = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(expected)))
        observed = get_shared_secret_from_filesystem()
        self.assertEqual(expected, observed)

    def test__prompts_user_for_secret(self):
        url = factory.make_simple_http_url()
        expected_previous_value = factory.make_bytes()
        set_shared_secret_on_filesystem(expected_previous_value)
        InstallSharedSecretScript_mock = self.patch(
            register_command, "InstallSharedSecretScript"
        )
        args = self.make_args(url=url, secret=None)
        register_command.run(args)
        observed = get_shared_secret_from_filesystem()

        self.expectThat(expected_previous_value, Equals(observed))
        self.expectThat(
            InstallSharedSecretScript_mock.run, MockCalledOnceWith(args)
        )

    def test__errors_out_when_piped_stdin_and_url_not_supplied(self):
        args = self.make_args(url=None)
        stdin = self.patch(register_command, "stdin")
        stdin.isatty.return_value = False
        self.assertRaises(SystemExit, register_command.run, args)

    def test__crashes_on_eoferror(self):
        args = self.make_args(url=None)
        stdin = self.patch(register_command, "stdin")
        stdin.isatty.return_value = True
        input = self.patch(register_command, "input")
        input.side_effect = EOFError
        self.assertRaises(SystemExit, register_command.run, args)

    def test__crashes_on_keyboardinterrupt(self):
        args = self.make_args(url=None)
        stdin = self.patch(register_command, "stdin")
        stdin.isatty.return_value = True
        input = self.patch(register_command, "input")
        input.side_effect = KeyboardInterrupt
        self.assertRaises(KeyboardInterrupt, register_command.run, args)

    def test__restarts_maas_rackd_service(self):
        url = factory.make_simple_http_url()
        secret = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(secret)))
        self.assertThat(
            self.mock_call_and_check,
            MockCallsMatch(
                call(["systemctl", "stop", "maas-rackd"]),
                call(["systemctl", "enable", "maas-rackd"]),
                call(["systemctl", "start", "maas-rackd"]),
            ),
        )

    def test__deletes_maas_id_file(self):
        self.useFixture(MAASIDFixture(factory.make_string()))
        url = factory.make_simple_http_url()
        secret = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(secret)))
        self.assertIsNone(get_maas_id())

    def test__show_service_stop_error(self):
        url = factory.make_simple_http_url()
        secret = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(secret)))
        mock_call_and_check = self.patch(register_command, "call_and_check")
        mock_call_and_check.side_effect = [
            ExternalProcessError(1, "systemctl stop", "mock error"),
            call(),
            call(),
        ]
        mock_stderr = self.patch(register_command.stderr, "write")
        with ExpectedException(SystemExit):
            register_command.run(
                self.make_args(url=url, secret=to_hex(secret))
            )
        self.assertThat(
            mock_stderr,
            MockCallsMatch(
                call("Unable to stop maas-rackd service."),
                call("\n"),
                call("Failed with error: mock error."),
                call("\n"),
            ),
        )

    def test__show_service_enable_error(self):
        url = factory.make_simple_http_url()
        secret = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(secret)))
        mock_call_and_check = self.patch(register_command, "call_and_check")
        mock_call_and_check.side_effect = [
            call(),
            ExternalProcessError(1, "systemctl enable", "mock error"),
            call(),
        ]
        mock_stderr = self.patch(register_command.stderr, "write")
        with ExpectedException(SystemExit):
            register_command.run(
                self.make_args(url=url, secret=to_hex(secret))
            )
        self.assertThat(
            mock_stderr,
            MockCallsMatch(
                call("Unable to enable and start the maas-rackd service."),
                call("\n"),
                call("Failed with error: mock error."),
                call("\n"),
            ),
        )

    def test__show_service_start_error(self):
        url = factory.make_simple_http_url()
        secret = factory.make_bytes()
        register_command.run(self.make_args(url=url, secret=to_hex(secret)))
        mock_call_and_check = self.patch(register_command, "call_and_check")
        mock_call_and_check.side_effect = [
            call(),
            call(),
            ExternalProcessError(1, "systemctl start", "mock error"),
        ]
        mock_stderr = self.patch(register_command.stderr, "write")
        with ExpectedException(SystemExit):
            register_command.run(
                self.make_args(url=url, secret=to_hex(secret))
            )
        self.assertThat(
            mock_stderr,
            MockCallsMatch(
                call("Unable to enable and start the maas-rackd service."),
                call("\n"),
                call("Failed with error: mock error."),
                call("\n"),
            ),
        )
