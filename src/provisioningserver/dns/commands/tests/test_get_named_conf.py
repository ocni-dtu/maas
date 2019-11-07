# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the get-named-conf command."""

__all__ = []

from argparse import ArgumentParser
import io

from maastesting.testcase import MAASTestCase
from provisioningserver.dns.commands.get_named_conf import add_arguments, run
from testtools.matchers import Contains, FileContains


class TestGetNamedConfCommand(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.output = io.StringIO()
        self.error_output = io.StringIO()
        self.parser = ArgumentParser()
        add_arguments(self.parser)

    def run_command(self, *args):
        parsed_args = self.parser.parse_args([*args])
        return run(parsed_args, stdout=self.output, stderr=self.error_output)

    def test_get_named_conf_returns_snippet(self):
        self.run_command()
        result = self.output.getvalue()
        # Just check that the returned snippet looks all right.
        self.assertIn('include "', result)

    def test_get_named_conf_appends_to_config_file(self):
        file_path = self.make_file()
        self.run_command("--edit", "--config-path", file_path)
        self.assertThat(file_path, FileContains(matcher=Contains('include "')))
