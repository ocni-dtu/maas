# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Integration-test the `maascli` command."""

__all__ = []

import os.path
import random
from subprocess import CalledProcessError, check_output, STDOUT
from textwrap import dedent

from maascli import main
from maascli.config import ProfileConfig
from maascli.testing.config import make_configs
from maascli.utils import handler_command_name
from maastesting import root
from maastesting.fixtures import CaptureStandardIO
from maastesting.matchers import DocTestMatches
from maastesting.testcase import MAASTestCase
from testtools.matchers import Equals


def locate_maascli():
    return os.path.join(root, "bin", "maas")


class TestMAASCli(MAASTestCase):
    def run_command(self, *args):
        check_output([locate_maascli()] + list(args), stderr=STDOUT)

    def test_run_without_args_fails(self):
        self.assertRaises(CalledProcessError, self.run_command)

    def test_run_without_args_shows_help_reminder(self):
        self.output_file = self.make_file("output")
        try:
            self.run_command()
        except CalledProcessError as error:
            self.assertIn(
                "Run %s --help for usage details." % locate_maascli(),
                error.output.decode("ascii"),
            )

    def test_help_option_succeeds(self):
        try:
            self.run_command("-h")
        except CalledProcessError as error:
            self.fail(error.output.decode("ascii"))
        else:
            # The test is that we get here without error.
            pass

    def test_list_command_succeeds(self):
        try:
            self.run_command("list")
        except CalledProcessError as error:
            self.fail(error.output.decode("ascii"))
        else:
            # The test is that we get here without error.
            pass


class TestMain(MAASTestCase):
    """Tests of `maascli.main` directly."""

    def fake_profile(self):
        """Fake a profile."""
        configs = make_configs()  # Instance of FakeConfig.
        self.patch(ProfileConfig, "open").return_value = configs
        return configs

    def test_complains_about_too_few_arguments(self):
        configs = self.fake_profile()
        [profile_name] = configs
        resources = configs[profile_name]["description"]["resources"]
        resource_name = random.choice(resources)["name"]
        command = "maas", profile_name, handler_command_name(resource_name)

        with CaptureStandardIO() as stdio:
            error = self.assertRaises(SystemExit, main, command)

        self.assertThat(error.code, Equals(2))
        self.assertThat(
            stdio.getError(),
            DocTestMatches(
                dedent(
                    """\
                usage: maas [-h] COMMAND ...
                <BLANKLINE>
                ...
                <BLANKLINE>
                too few arguments
                """
                )
            ),
        )
