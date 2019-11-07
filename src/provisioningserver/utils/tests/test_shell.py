# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for utilities to execute external commands."""

__all__ = []

import os
import random
from subprocess import CalledProcessError

from fixtures import EnvironmentVariable
from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from provisioningserver.utils.shell import (
    call_and_check,
    ExternalProcessError,
    get_env_with_bytes_locale,
    get_env_with_locale,
    has_command_available,
)
import provisioningserver.utils.shell as shell_module
from testtools.matchers import ContainsDict, Equals, Is, IsInstance, Not


class TestCallAndCheck(MAASTestCase):
    """Tests `call_and_check`."""

    def patch_popen(self, returncode=0, stderr=""):
        """Replace `subprocess.Popen` with a mock."""
        popen = self.patch(shell_module, "Popen")
        process = popen.return_value
        process.communicate.return_value = (None, stderr)
        process.returncode = returncode
        return process

    def test__returns_standard_output(self):
        output = factory.make_string().encode("ascii")
        self.assertEqual(output, call_and_check(["/bin/echo", "-n", output]))

    def test__raises_ExternalProcessError_on_failure(self):
        command = factory.make_name("command")
        message = factory.make_string()
        self.patch_popen(returncode=1, stderr=message)
        error = self.assertRaises(
            ExternalProcessError, call_and_check, command
        )
        self.assertEqual(1, error.returncode)
        self.assertEqual(command, error.cmd)
        self.assertEqual(message, error.output)

    def test__passes_timeout_to_communicate(self):
        command = factory.make_name("command")
        process = self.patch_popen()
        timeout = random.randint(1, 10)
        call_and_check(command, timeout=timeout)
        self.assertThat(
            process.communicate, MockCalledOnceWith(timeout=timeout)
        )

    def test__reports_stderr_on_failure(self):
        nonfile = os.path.join(self.make_dir(), factory.make_name("nonesuch"))
        error = self.assertRaises(
            ExternalProcessError,
            call_and_check,
            ["/bin/cat", nonfile],
            env={"LC_ALL": "C"},
        )
        self.assertEqual(
            b"/bin/cat: %s: No such file or directory"
            % nonfile.encode("ascii"),
            error.output,
        )


class TestExternalProcessError(MAASTestCase):
    """Tests for the ExternalProcessError class."""

    def test_upgrade_upgrades_CalledProcessError(self):
        error = factory.make_CalledProcessError()
        self.expectThat(error, Not(IsInstance(ExternalProcessError)))
        ExternalProcessError.upgrade(error)
        self.expectThat(error, IsInstance(ExternalProcessError))

    def test_upgrade_does_not_change_CalledProcessError_subclasses(self):
        error_type = factory.make_exception_type(bases=(CalledProcessError,))
        error = factory.make_CalledProcessError()
        error.__class__ = error_type  # Change the class.
        self.expectThat(error, Not(IsInstance(ExternalProcessError)))
        ExternalProcessError.upgrade(error)
        self.expectThat(error, Not(IsInstance(ExternalProcessError)))
        self.expectThat(error.__class__, Is(error_type))

    def test_upgrade_does_not_change_other_errors(self):
        error_type = factory.make_exception_type()
        error = error_type()
        self.expectThat(error, Not(IsInstance(ExternalProcessError)))
        ExternalProcessError.upgrade(error)
        self.expectThat(error, Not(IsInstance(ExternalProcessError)))
        self.expectThat(error.__class__, Is(error_type))

    def test_upgrade_returns_None(self):
        self.expectThat(
            ExternalProcessError.upgrade(factory.make_exception()), Is(None)
        )

    def test_to_unicode_decodes_to_unicode(self):
        # Byte strings are decoded as ASCII by _to_unicode(), replacing
        # all non-ASCII characters with U+FFFD REPLACEMENT CHARACTERs.
        byte_string = b"This string will be converted. \xe5\xb2\x81\xe5."
        expected_unicode_string = (
            "This string will be converted. \ufffd\ufffd\ufffd\ufffd."
        )
        converted_string = ExternalProcessError._to_unicode(byte_string)
        self.assertIsInstance(converted_string, str)
        self.assertEqual(expected_unicode_string, converted_string)

    def test_to_unicode_defers_to_unicode_constructor(self):
        # Unicode strings and non-byte strings are handed to unicode()
        # to undergo Python's normal coercion strategy. (For unicode
        # strings this is actually a no-op, but it's cheaper to do this
        # than special-case unicode strings.)
        self.assertEqual(str(self), ExternalProcessError._to_unicode(self))

    def test_to_ascii_encodes_to_bytes(self):
        # Yes, this is how you really spell "smorgasbord."  Look it up.
        unicode_string = "Sm\xf6rg\xe5sbord"
        expected_byte_string = b"Sm?rg?sbord"
        converted_string = ExternalProcessError._to_ascii(unicode_string)
        self.assertIsInstance(converted_string, bytes)
        self.assertEqual(expected_byte_string, converted_string)

    def test_to_ascii_defers_to_bytes(self):
        # Byte strings and non-unicode strings are handed to bytes() to
        # undergo Python's normal coercion strategy. (For byte strings
        # this is actually a no-op, but it's cheaper to do this than
        # special-case byte strings.)
        self.assertEqual(
            str(self).encode("ascii"), ExternalProcessError._to_ascii(self)
        )

    def test_to_ascii_removes_non_printable_chars(self):
        # After conversion to a byte string, all non-printable and
        # non-ASCII characters are replaced with question marks.
        byte_string = b"*How* many roads\x01\x02\xb2\xfe"
        expected_byte_string = b"*How* many roads????"
        converted_string = ExternalProcessError._to_ascii(byte_string)
        self.assertIsInstance(converted_string, bytes)
        self.assertEqual(expected_byte_string, converted_string)

    def test__str__returns_unicode(self):
        error = ExternalProcessError(returncode=-1, cmd="foo-bar")
        self.assertIsInstance(error.__str__(), str)

    def test__str__contains_output(self):
        output = b"Mot\xf6rhead"
        unicode_output = "Mot\ufffdrhead"
        error = ExternalProcessError(
            returncode=-1, cmd="foo-bar", output=output
        )
        self.assertIn(unicode_output, error.__str__())

    def test_output_as_ascii(self):
        output = b"Joyeux No\xebl"
        ascii_output = b"Joyeux No?l"
        error = ExternalProcessError(
            returncode=-1, cmd="foo-bar", output=output
        )
        self.assertEqual(ascii_output, error.output_as_ascii)

    def test_output_as_unicode(self):
        output = b"Mot\xf6rhead"
        unicode_output = "Mot\ufffdrhead"
        error = ExternalProcessError(
            returncode=-1, cmd="foo-bar", output=output
        )
        self.assertEqual(unicode_output, error.output_as_unicode)


class TestHasCommandAvailable(MAASTestCase):
    def test__calls_which(self):
        mock_call_and_check = self.patch(shell_module, "call_and_check")
        cmd = factory.make_name("cmd")
        has_command_available(cmd)
        self.assertThat(
            mock_call_and_check, MockCalledOnceWith(["which", cmd])
        )

    def test__returns_False_when_ExternalProcessError_raised(self):
        self.patch(
            shell_module, "call_and_check"
        ).side_effect = ExternalProcessError(1, "cmd")
        self.assertFalse(has_command_available(factory.make_name("cmd")))

    def test__returns_True_when_ExternalProcessError_not_raised(self):
        self.patch(shell_module, "call_and_check")
        self.assertTrue(has_command_available(factory.make_name("cmd")))


# Taken from locale(7).
LC_VAR_NAMES = {
    "LC_ADDRESS",
    "LC_COLLATE",
    "LC_CTYPE",
    "LC_IDENTIFICATION",
    "LC_MONETARY",
    "LC_MESSAGES",
    "LC_MEASUREMENT",
    "LC_NAME",
    "LC_NUMERIC",
    "LC_PAPER",
    "LC_TELEPHONE",
    "LC_TIME",
}


class TestGetEnvWithLocale(MAASTestCase):
    """Tests for `get_env_with_locale`."""

    def test__sets_LANG_and_LC_ALL(self):
        self.assertThat(
            get_env_with_locale({}),
            Equals(
                {"LANG": "C.UTF-8", "LANGUAGE": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            ),
        )

    def test__overwrites_LANG(self):
        self.assertThat(
            get_env_with_locale({"LANG": factory.make_name("LANG")}),
            Equals(
                {"LANG": "C.UTF-8", "LANGUAGE": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            ),
        )

    def test__overwrites_LANGUAGE(self):
        self.assertThat(
            get_env_with_locale({"LANGUAGE": factory.make_name("LANGUAGE")}),
            Equals(
                {"LANG": "C.UTF-8", "LANGUAGE": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            ),
        )

    def test__removes_other_LC_variables(self):
        self.assertThat(
            get_env_with_locale(
                {name: factory.make_name(name) for name in LC_VAR_NAMES}
            ),
            Equals(
                {"LANG": "C.UTF-8", "LANGUAGE": "C.UTF-8", "LC_ALL": "C.UTF-8"}
            ),
        )

    def test__passes_other_variables_through(self):
        basis = {
            factory.make_name("name"): factory.make_name("value")
            for _ in range(5)
        }
        expected = basis.copy()
        expected["LANG"] = expected["LC_ALL"] = expected[
            "LANGUAGE"
        ] = "C.UTF-8"
        observed = get_env_with_locale(basis)
        self.assertThat(observed, Equals(expected))

    def test__defaults_to_process_environment(self):
        name = factory.make_name("name")
        value = factory.make_name("value")
        with EnvironmentVariable(name, value):
            self.assertThat(
                get_env_with_locale(), ContainsDict({name: Equals(value)})
            )


class TestGetEnvWithBytesLocale(MAASTestCase):
    """Tests for `get_env_with_bytes_locale`."""

    def test__sets_LANG_and_LC_ALL(self):
        self.assertThat(
            get_env_with_bytes_locale({}),
            Equals(
                {
                    b"LANG": b"C.UTF-8",
                    b"LANGUAGE": b"C.UTF-8",
                    b"LC_ALL": b"C.UTF-8",
                }
            ),
        )

    def test__overwrites_LANG(self):
        self.assertThat(
            get_env_with_bytes_locale(
                {b"LANG": factory.make_name("LANG").encode("ascii")}
            ),
            Equals(
                {
                    b"LANG": b"C.UTF-8",
                    b"LANGUAGE": b"C.UTF-8",
                    b"LC_ALL": b"C.UTF-8",
                }
            ),
        )

    def test__overwrites_LANGUAGE(self):
        self.assertThat(
            get_env_with_bytes_locale(
                {b"LANGUAGE": factory.make_name("LANGUAGE").encode("ascii")}
            ),
            Equals(
                {
                    b"LANG": b"C.UTF-8",
                    b"LANGUAGE": b"C.UTF-8",
                    b"LC_ALL": b"C.UTF-8",
                }
            ),
        )

    def test__removes_other_LC_variables(self):
        self.assertThat(
            get_env_with_bytes_locale(
                {
                    name.encode("ascii"): factory.make_name(name).encode(
                        "ascii"
                    )
                    for name in LC_VAR_NAMES
                }
            ),
            Equals(
                {
                    b"LANG": b"C.UTF-8",
                    b"LANGUAGE": b"C.UTF-8",
                    b"LC_ALL": b"C.UTF-8",
                }
            ),
        )

    def test__passes_other_variables_through(self):
        basis = {
            factory.make_name("name").encode("ascii"): (
                factory.make_name("value").encode("ascii")
            )
            for _ in range(5)
        }
        expected = basis.copy()
        expected[b"LANG"] = expected[b"LC_ALL"] = expected[
            b"LANGUAGE"
        ] = b"C.UTF-8"
        observed = get_env_with_bytes_locale(basis)
        self.assertThat(observed, Equals(expected))

    def test__defaults_to_process_environment(self):
        name = factory.make_name("name")
        value = factory.make_name("value")
        with EnvironmentVariable(name, value):
            self.assertThat(
                get_env_with_bytes_locale(),
                ContainsDict(
                    {name.encode("ascii"): Equals(value.encode("ascii"))}
                ),
            )
