# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ``provisioningserver.utils.avahi``."""

__all__ = []

from argparse import ArgumentParser
from contextlib import contextmanager
import io
import json
import subprocess
from tempfile import NamedTemporaryFile
import time

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from provisioningserver.utils import avahi as avahi_module
from provisioningserver.utils.avahi import (
    add_arguments,
    parse_avahi_event,
    run,
    unescape_avahi_service_name,
)
from testtools.matchers import Equals, HasLength, Not
from testtools.testcase import ExpectedException


class TestUnescapeAvahiServiceName(MAASTestCase):
    def test__converts_escaped_decimal_characters(self):
        result = unescape_avahi_service_name(
            "HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041"
        )
        self.assertThat(result, Equals("HP Color LaserJet CP2025dn (test)"))

    def test__converts_escaped_backslash(self):
        result = unescape_avahi_service_name("\\\\\\\\samba\\\\share")
        self.assertThat(result, Equals("\\\\samba\\share"))

    def test__converts_escaped_dot(self):
        result = unescape_avahi_service_name("example\\.com")
        self.assertThat(result, Equals("example.com"))

    def test__converts_all_types_of_escape_sequences(self):
        result = unescape_avahi_service_name(
            "HP\\032Color\\032LaserJet\\032at"
            "\\032\\\\\\\\printers\\\\color\\032\\040example\\.com\\041"
        )
        self.assertThat(
            result,
            Equals("HP Color LaserJet at \\\\printers\\color (example.com)"),
        )


class TestParseAvahiEvent(MAASTestCase):
    def test__parses_browser_new_event(self):
        input = (
            b"+;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local"
        )
        event = parse_avahi_event(input)
        self.assertEquals(
            event,
            {
                "event": "BROWSER_NEW",
                "interface": "eth0",
                "protocol": "IPv4",
                "service_name": "HP Color LaserJet CP2025dn (test)",
                "type": "_http._tcp",
                "domain": "local",
            },
        )

    def test__parses_browser_removed_event(self):
        input = (
            b"-;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local"
        )
        event = parse_avahi_event(input)
        self.assertEquals(
            event,
            {
                "event": "BROWSER_REMOVED",
                "interface": "eth0",
                "protocol": "IPv4",
                "service_name": "HP Color LaserJet CP2025dn (test)",
                "type": "_http._tcp",
                "domain": "local",
            },
        )

    def test__parses_resolver_found_event(self):
        input = (
            b"=;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local;"
            b"printer.local;"
            b"192.168.0.222;"
            b"80;"
            b'"priority=50" "rp=RAW"'
        )
        event = parse_avahi_event(input)
        self.assertEquals(
            event,
            {
                "event": "RESOLVER_FOUND",
                "interface": "eth0",
                "protocol": "IPv4",
                "service_name": "HP Color LaserJet CP2025dn (test)",
                "type": "_http._tcp",
                "domain": "local",
                "address": "192.168.0.222",
                "fqdn": "printer.local",
                "hostname": "printer",
                "port": "80",
                "txt": b'"priority=50" "rp=RAW"',
            },
        )

    def test__parses_txt_binary(self):
        input = (
            b"=;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local;"
            b"printer.local;"
            b"192.168.0.222;"
            b"80;"
            b'"BluetoothAddress=\xc8i\xcdB\xe2\x09"'
        )
        event = parse_avahi_event(input)
        self.assertEquals(
            b'"BluetoothAddress=\xc8i\xcdB\xe2\x09"', event["txt"]
        )

    def test__returns_none_for_malformed_input(self):
        self.assertThat(parse_avahi_event(b";;;"), Equals(None))


def observe_mdns(*, input, output, verbose=False):
    """Print avahi hostname bindings on stdout.

    This is a backwards-compatibility shim to aid testing.
    """

    @contextmanager
    def reader():
        yield input

    return avahi_module._observe_mdns(reader(), output, verbose=verbose)


class TestObserveMDNS(MAASTestCase):
    def test__prints_event_json_in_verbose_mode(self):
        out = io.StringIO()
        input = (
            b"+;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local\n"
        )
        expected_result = {
            "event": "BROWSER_NEW",
            "interface": "eth0",
            "protocol": "IPv4",
            "service_name": "HP Color LaserJet CP2025dn (test)",
            "type": "_http._tcp",
            "domain": "local",
        }
        observe_mdns(verbose=True, input=[input], output=out)
        output = io.StringIO(out.getvalue())
        lines = output.readlines()
        self.assertThat(lines, HasLength(1))
        self.assertThat(json.loads(lines[0]), Equals(expected_result))

    def test__skips_unimportant_events_without_verbose_enabled(self):
        out = io.StringIO()
        input = (
            b"+;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local\n"
        )
        observe_mdns(verbose=False, input=[input], output=out)
        output = io.StringIO(out.getvalue())
        lines = output.readlines()
        self.assertThat(lines, HasLength(0))

    def test__non_verbose_removes_redundant_events_and_outputs_summary(self):
        out = io.StringIO()
        input = (
            b"=;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local;"
            b"printer.local;"
            b"192.168.0.222;"
            b"80;"
            b'"priority=50" "rp=RAW"\n'
        )
        observe_mdns(verbose=False, input=[input, input], output=out)
        output = io.StringIO(out.getvalue())
        lines = output.readlines()
        self.assertThat(lines, HasLength(1))
        self.assertThat(
            json.loads(lines[0]),
            Equals(
                {
                    "interface": "eth0",
                    "address": "192.168.0.222",
                    "hostname": "printer",
                }
            ),
        )

    def test__non_verbose_removes_waits_before_emitting_duplicate_entry(self):
        out = io.StringIO()
        input = (
            b"=;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local;"
            b"printer.local;"
            b"192.168.0.222;"
            b"80;"
            b'"priority=50" "rp=RAW"\n'
        )
        # If we see the same entry 3 times over the course of 15 minutes, we
        # should only see output two out of the three times.
        self.patch(time, "monotonic").side_effect = (100.0, 200.0, 900.0)
        observe_mdns(verbose=False, input=[input, input, input], output=out)
        output = io.StringIO(out.getvalue())
        lines = output.readlines()
        self.assertThat(lines, HasLength(2))
        self.assertThat(
            json.loads(lines[0]),
            Equals(
                {
                    "interface": "eth0",
                    "address": "192.168.0.222",
                    "hostname": "printer",
                }
            ),
        )
        self.assertThat(
            json.loads(lines[1]),
            Equals(
                {
                    "interface": "eth0",
                    "address": "192.168.0.222",
                    "hostname": "printer",
                }
            ),
        )


class TestObserveMDNSCommand(MAASTestCase):
    """Tests for `maas-rack observe-mdns`."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_input_bytes = (
            b"=;eth0;IPv4"
            b";HP\\032Color\\032LaserJet\\032CP2025dn\\032\\040test\\041;"
            b"_http._tcp;local;"
            b"printer.local;"
            b"192.168.0.222;"
            b"80;"
            b'"priority=50" "rp=RAW"\n'
        )

    def test__calls_subprocess_by_default(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args([])
        popen = self.patch(avahi_module.subprocess, "Popen")
        popen.return_value.stdout = io.BytesIO(self.test_input_bytes)
        popen.return_value.wait.return_value = 0
        popen.return_value.returncode = 0
        output = io.StringIO()
        run(args, output=output)
        self.assertThat(
            popen,
            MockCalledOnceWith(
                [
                    "/usr/bin/avahi-browse",
                    "--all",
                    "--resolve",
                    "--no-db-lookup",
                    "--parsable",
                    "--no-fail",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            ),
        )

    def test__allows_pipe_input(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["--input-file", "-"])
        output = io.StringIO()
        run(args, output=output, stdin=[self.test_input_bytes])
        self.assertThat(output.getvalue(), Not(HasLength(0)))

    def test__allows_file_input(self):
        with NamedTemporaryFile("wb") as f:
            parser = ArgumentParser()
            add_arguments(parser)
            f.write(self.test_input_bytes)
            f.flush()
            args = parser.parse_args(["--input-file", f.name])
            output = io.StringIO()
            run(args, output=output)

    def test__raises_systemexit(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args([])
        popen = self.patch(avahi_module.subprocess, "Popen")
        popen.return_value.wait.return_value = 42
        popen.return_value.returncode = 42
        popen.return_value.stdout = io.BytesIO(self.test_input_bytes)
        output = io.StringIO()
        with ExpectedException(SystemExit, ".*42.*"):
            run(args, output=output)

    def test__sets_self_as_process_group_leader(self):
        exception_type = factory.make_exception_type()
        os = self.patch(avahi_module, "os")
        os.setpgrp.side_effect = exception_type
        self.assertRaises(exception_type, run, [])
        self.assertThat(os.setpgrp, MockCalledOnceWith())
