# Copyright 2014-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for filesystem-related utilities."""

__all__ = []

import io
import os
import os.path
import random
from shutil import rmtree
import stat
from subprocess import CalledProcessError, PIPE
import tempfile
import time
import tokenize
import types
from unittest.mock import ANY, call, create_autospec, Mock, sentinel

from maastesting import root
from maastesting.factory import factory
from maastesting.fixtures import CaptureStandardIO
from maastesting.matchers import (
    DocTestMatches,
    FileContains,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from maastesting.utils import age_file
import provisioningserver.config
from provisioningserver.path import get_data_path, get_tentative_data_path
from provisioningserver.utils.fs import (
    atomic_copy,
    atomic_delete,
    atomic_symlink,
    atomic_write,
    FileLock,
    get_library_script_path,
    get_maas_common_command,
    incremental_write,
    NamedLock,
    read_text_file,
    RunLock,
    sudo_delete_file,
    sudo_write_file,
    SystemLock,
    tempdir,
    write_text_file,
)
import provisioningserver.utils.fs as fs_module
from testtools.matchers import (
    AllMatch,
    DirContains,
    DirExists,
    EndsWith,
    Equals,
    FileExists,
    GreaterThan,
    HasLength,
    IsInstance,
    MatchesRegex,
    Not,
    SamePath,
    StartsWith,
)
from testtools.testcase import ExpectedException
from twisted import internet
from twisted.internet.task import Clock
from twisted.python import lockfile


class TestAtomicWrite(MAASTestCase):
    """Test `atomic_write`."""

    def test_atomic_write_overwrites_dest_file(self):
        content = factory.make_bytes()
        filename = self.make_file(contents=factory.make_string())
        atomic_write(content, filename)
        self.assertThat(filename, FileContains(content))

    def test_atomic_write_does_not_overwrite_file_if_overwrite_false(self):
        content = factory.make_bytes()
        random_content = factory.make_bytes()
        filename = self.make_file(contents=random_content)
        atomic_write(content, filename, overwrite=False)
        self.assertThat(filename, FileContains(random_content))

    def test_atomic_write_writes_file_if_no_file_present(self):
        filename = os.path.join(self.make_dir(), factory.make_string())
        content = factory.make_bytes()
        atomic_write(content, filename, overwrite=False)
        self.assertThat(filename, FileContains(content))

    def test_atomic_write_does_not_leak_temp_file_when_not_overwriting(self):
        # If the file is not written because it already exists and
        # overwriting was disabled, atomic_write does not leak its
        # temporary file.
        filename = self.make_file()
        atomic_write(factory.make_bytes(), filename, overwrite=False)
        self.assertEqual(
            [os.path.basename(filename)], os.listdir(os.path.dirname(filename))
        )

    def test_atomic_write_does_not_leak_temp_file_on_failure(self):
        # If the overwrite fails, atomic_write does not leak its
        # temporary file.
        self.patch(fs_module, "rename", Mock(side_effect=OSError()))
        filename = self.make_file()
        with ExpectedException(OSError):
            atomic_write(factory.make_bytes(), filename)
        self.assertEqual(
            [os.path.basename(filename)], os.listdir(os.path.dirname(filename))
        )

    def test_atomic_write_sets_permissions(self):
        atomic_file = self.make_file()
        # Pick an unusual mode that is also likely to fall outside our
        # umask.  We want this mode set, not treated as advice that may
        # be tightened up by umask later.
        mode = 0o323
        atomic_write(factory.make_bytes(), atomic_file, mode=mode)
        self.assertEqual(mode, stat.S_IMODE(os.stat(atomic_file).st_mode))

    def test_atomic_write_sets_permissions_before_moving_into_place(self):

        recorded_modes = []

        def record_mode(source, dest):
            """Stub for os.rename: get source file's access mode."""
            recorded_modes.append(os.stat(source).st_mode)

        self.patch(fs_module, "rename", Mock(side_effect=record_mode))
        playground = self.make_dir()
        atomic_file = os.path.join(playground, factory.make_name("atomic"))
        mode = 0o323
        atomic_write(factory.make_bytes(), atomic_file, mode=mode)
        [recorded_mode] = recorded_modes
        self.assertEqual(mode, stat.S_IMODE(recorded_mode))

    def test_atomic_write_preserves_ownership_before_moving_into_place(self):
        atomic_file = self.make_file("atomic")

        self.patch(fs_module, "isfile").return_value = True
        self.patch(fs_module, "chown")
        self.patch(fs_module, "rename")
        self.patch(fs_module, "stat")

        ret_stat = fs_module.stat.return_value
        ret_stat.st_uid = sentinel.uid
        ret_stat.st_gid = sentinel.gid
        ret_stat.st_mode = stat.S_IFREG

        atomic_write(factory.make_bytes(), atomic_file)

        self.assertThat(fs_module.stat, MockCalledOnceWith(atomic_file))
        self.assertThat(
            fs_module.chown,
            MockCalledOnceWith(ANY, sentinel.uid, sentinel.gid),
        )

    def test_atomic_write_sets_OSError_filename_if_undefined(self):
        # When the filename attribute of an OSError is undefined when
        # attempting to create a temporary file, atomic_write fills it in with
        # a representative filename, similar to the specification required by
        # mktemp(1).
        mock_mkstemp = self.patch(tempfile, "mkstemp")
        mock_mkstemp.side_effect = OSError()
        filename = os.path.join("directory", "basename")
        error = self.assertRaises(OSError, atomic_write, b"content", filename)
        self.assertEqual(
            os.path.join("directory", ".basename.XXXXXX.tmp"), error.filename
        )

    def test_atomic_write_does_not_set_OSError_filename_if_defined(self):
        # When the filename attribute of an OSError is defined when attempting
        # to create a temporary file, atomic_write leaves it alone.
        mock_mkstemp = self.patch(tempfile, "mkstemp")
        mock_mkstemp.side_effect = OSError()
        mock_mkstemp.side_effect.filename = factory.make_name("filename")
        filename = os.path.join("directory", "basename")
        error = self.assertRaises(OSError, atomic_write, b"content", filename)
        self.assertEqual(mock_mkstemp.side_effect.filename, error.filename)

    def test_atomic_write_rejects_non_bytes_contents(self):
        self.assertRaises(
            TypeError,
            atomic_write,
            factory.make_string(),
            factory.make_string(),
        )


class TestAtomicCopy(MAASTestCase):
    def test_integration(self):
        loader_contents = factory.make_bytes()
        loader = self.make_file(contents=loader_contents)
        destination = self.make_file()
        atomic_copy(loader, destination)
        self.assertThat(destination, FileContains(loader_contents))

    def test___installs_new_bootloader(self):
        contents = factory.make_bytes()
        loader = self.make_file(contents=contents)
        install_dir = self.make_dir()
        dest = os.path.join(install_dir, factory.make_name("loader"))
        atomic_copy(loader, dest)
        self.assertThat(dest, FileContains(contents))

    def test__replaces_file_if_changed(self):
        contents = factory.make_bytes()
        loader = self.make_file(contents=contents)
        dest = self.make_file(contents="Old contents")
        atomic_copy(loader, dest)
        self.assertThat(dest, FileContains(contents))

    def test__skips_if_unchanged(self):
        contents = factory.make_bytes()
        dest = self.make_file(contents=contents)
        age_file(dest, 100)
        original_write_time = os.stat(dest).st_mtime
        loader = self.make_file(contents=contents)
        atomic_copy(loader, dest)
        self.assertThat(dest, FileContains(contents))
        self.assertEqual(original_write_time, os.stat(dest).st_mtime)

    def test__sweeps_aside_dot_new_if_any(self):
        contents = factory.make_bytes()
        loader = self.make_file(contents=contents)
        dest = self.make_file(contents="Old contents")
        temp_file = "%s.new" % dest
        factory.make_file(
            os.path.dirname(temp_file), name=os.path.basename(temp_file)
        )
        atomic_copy(loader, dest)
        self.assertThat(dest, FileContains(contents))


class TestAtomicDelete(MAASTestCase):
    """Test `atomic_delete`."""

    def test_atomic_delete_deletes_file(self):
        filename = self.make_file()
        atomic_delete(filename)
        self.assertThat(filename, Not(FileExists()))

    def test_renames_file_before_deleting(self):
        # Intercept calls to os.remove.
        os_remove = self.patch(fs_module.os, "remove")

        contents = factory.make_name("contents").encode("ascii")
        filepath = self.make_file(contents=contents)
        filedir = os.path.dirname(filepath)

        atomic_delete(filepath)

        listing = os.listdir(filedir)
        self.assertThat(listing, HasLength(1))
        [deletedname] = listing
        self.assertThat(deletedname, MatchesRegex(r"^[.][^.]+[.]deleted$"))
        deletedpath = os.path.join(filedir, deletedname)
        self.assertThat(os_remove, MockCalledOnceWith(deletedpath))
        self.assertThat(deletedpath, FileContains(contents))

    def test_leaves_nothing_behind_on_error(self):
        dirname = self.make_dir()
        filename = os.path.join(dirname, "does-not-exist")
        self.assertRaises(FileNotFoundError, atomic_delete, filename)
        self.assertThat(dirname, DirContains([]))


class TestAtomicSymlink(MAASTestCase):
    """Test `atomic_symlink`."""

    def test_atomic_symlink_creates_symlink(self):
        filename = self.make_file(contents=factory.make_string())
        target_dir = self.make_dir()
        link_name = factory.make_name("link")
        target = os.path.join(target_dir, link_name)
        atomic_symlink(filename, target)
        self.assertTrue(
            os.path.islink(target), "atomic_symlink didn't create a symlink"
        )
        self.assertThat(target, SamePath(filename))

    def test_atomic_symlink_overwrites_dest_file(self):
        filename = self.make_file(contents=factory.make_string())
        target_dir = self.make_dir()
        link_name = factory.make_name("link")
        # Create a file that will be overwritten.
        factory.make_file(location=target_dir, name=link_name)
        target = os.path.join(target_dir, link_name)
        atomic_symlink(filename, target)
        self.assertTrue(
            os.path.islink(target), "atomic_symlink didn't create a symlink"
        )
        self.assertThat(target, SamePath(filename))

    def test_atomic_symlink_does_not_leak_temp_file_if_failure(self):
        # In the face of failure, no temp file is leaked.
        self.patch(os, "rename", Mock(side_effect=OSError()))
        filename = self.make_file()
        target_dir = self.make_dir()
        link_name = factory.make_name("link")
        target = os.path.join(target_dir, link_name)
        with ExpectedException(OSError):
            atomic_symlink(filename, target)
        self.assertEqual([], os.listdir(target_dir))

    def test_atomic_symlink_uses_relative_path(self):
        filename = self.make_file(contents=factory.make_string())
        link_name = factory.make_name("link")
        target = os.path.join(os.path.dirname(filename), link_name)
        atomic_symlink(filename, target)
        self.assertEquals(os.path.basename(filename), os.readlink(target))
        self.assertTrue(os.path.samefile(filename, target))

    def test_atomic_symlink_uses_relative_path_for_directory(self):
        target_path = self.make_dir()  # The target is a directory.
        link_path = os.path.join(self.make_dir(), factory.make_name("sub"))
        atomic_symlink(target_path, link_path)
        self.assertThat(
            os.readlink(link_path),
            Equals(os.path.relpath(target_path, os.path.dirname(link_path))),
        )
        self.assertTrue(os.path.samefile(target_path, link_path))


class TestIncrementalWrite(MAASTestCase):
    """Test `incremental_write`."""

    def test_incremental_write_updates_modification_time(self):
        content = factory.make_bytes()
        filename = self.make_file(contents=factory.make_string())
        # Pretend that this file is older than it is.  So that
        # incrementing its mtime won't put it in the future.
        old_mtime = os.stat(filename).st_mtime - 10
        os.utime(filename, (old_mtime, old_mtime))
        incremental_write(content, filename)
        new_time = time.time()
        # should be much closer to new_time than to old_mtime.
        self.assertAlmostEqual(os.stat(filename).st_mtime, new_time, delta=2.0)

    def test_incremental_write_does_not_set_future_time(self):
        content = factory.make_bytes()
        filename = self.make_file(contents=factory.make_string())
        # Pretend that this file is older than it is.  So that
        # incrementing its mtime won't put it in the future.
        old_mtime = os.stat(filename).st_mtime + 10
        os.utime(filename, (old_mtime, old_mtime))
        incremental_write(content, filename)
        new_time = time.time()
        self.assertAlmostEqual(os.stat(filename).st_mtime, new_time, delta=2.0)

    def test_incremental_write_sets_permissions(self):
        atomic_file = self.make_file()
        mode = 0o323
        incremental_write(factory.make_bytes(), atomic_file, mode=mode)
        self.assertEqual(mode, stat.S_IMODE(os.stat(atomic_file).st_mode))


class TestGetMAASProvisionCommand(MAASTestCase):
    def test__returns_just_command_for_production(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = False
        self.assertEqual(
            "/usr/lib/maas/maas-common", get_maas_common_command()
        )

    def test__returns_maas_rack_for_snap(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = False
        self.patch(os, "environ", {"SNAP": "/snap/maas/10"})
        self.assertEqual(
            get_maas_common_command(), "/snap/maas/10/bin/maas-rack"
        )

    def test__returns_full_path_for_development(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = True
        self.assertEqual(
            root.rstrip("/") + "/bin/maas-common", get_maas_common_command()
        )


class TestGetLibraryScriptPath(MAASTestCase):
    """Tests for `get_library_script_path`."""

    def test__returns_usr_lib_maas_name_for_production(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = False
        script_name = factory.make_name("script")
        self.assertEqual(
            "/usr/lib/maas/" + script_name,
            get_library_script_path(script_name),
        )

    def test__returns_full_path_for_development(self):
        self.patch(provisioningserver.config, "is_dev_environment")
        provisioningserver.config.is_dev_environment.return_value = True
        script_name = factory.make_name("script")
        self.assertEqual(
            root.rstrip("/") + "/scripts/" + script_name,
            get_library_script_path(script_name),
        )


def patch_popen(test, returncode=0):
    process = test.patch_autospec(fs_module, "Popen").return_value
    process.communicate.return_value = "output", "error output"
    process.returncode = returncode
    return process


def patch_sudo(test):
    # Ensure that the `sudo` function always prepends a call to `sudo -n` to
    # the command; is_dev_environment will otherwise influence it.
    sudo = test.patch_autospec(fs_module, "sudo")
    sudo.side_effect = lambda command: ["sudo", "-n", *command]


def patch_dev(test, is_dev_environment):
    ide = test.patch_autospec(provisioningserver.config, "is_dev_environment")
    ide.return_value = bool(is_dev_environment)


class TestSudoWriteFile(MAASTestCase):
    """Testing for `sudo_write_file`."""

    def test_calls_atomic_write(self):
        patch_popen(self)
        patch_sudo(self)
        patch_dev(self, False)

        path = os.path.join(self.make_dir(), factory.make_name("file"))
        contents = factory.make_bytes()
        sudo_write_file(path, contents)

        self.assertThat(
            fs_module.Popen,
            MockCalledOnceWith(
                [
                    "sudo",
                    "-n",
                    get_library_script_path("maas-write-file"),
                    path,
                    "0644",
                ],
                stdin=PIPE,
            ),
        )

    def test_calls_atomic_write_dev_mode(self):
        patch_popen(self)
        patch_dev(self, True)

        path = os.path.join(self.make_dir(), factory.make_name("file"))
        contents = factory.make_bytes()
        sudo_write_file(path, contents)

        called_command = fs_module.Popen.call_args[0][0]
        self.assertNotIn("sudo", called_command)

    def test_rejects_non_bytes_contents(self):
        self.assertRaises(
            TypeError, sudo_write_file, self.make_file(), factory.make_string()
        )

    def test_catches_failures(self):
        patch_popen(self, 1)
        self.assertRaises(
            CalledProcessError,
            sudo_write_file,
            self.make_file(),
            factory.make_bytes(),
        )

    def test_can_write_file_in_development(self):
        filename = get_data_path("/var/lib/maas/dhcpd.conf")
        contents = factory.make_bytes()  # Binary safe.
        mode = random.randint(0o000, 0o777) | 0o400  # Always u+r.
        sudo_write_file(filename, contents, mode)
        self.assertThat(filename, FileContains(contents))
        self.assertThat(os.stat(filename).st_mode & 0o777, Equals(mode))


class TestSudoDeleteFile(MAASTestCase):
    """Testing for `sudo_delete_file`."""

    def test_calls_atomic_delete(self):
        patch_popen(self)
        patch_sudo(self)
        patch_dev(self, False)

        path = os.path.join(self.make_dir(), factory.make_name("file"))
        sudo_delete_file(path)

        self.assertThat(
            fs_module.Popen,
            MockCalledOnceWith(
                [
                    "sudo",
                    "-n",
                    get_library_script_path("maas-delete-file"),
                    path,
                ]
            ),
        )

    def test_calls_atomic_delete_dev_mode(self):
        patch_popen(self)
        patch_dev(self, True)

        path = os.path.join(self.make_dir(), factory.make_name("file"))
        sudo_delete_file(path)

        called_command = fs_module.Popen.call_args[0][0]
        self.assertNotIn("sudo", called_command)

    def test_catches_failures(self):
        patch_popen(self, 1)
        self.assertRaises(
            CalledProcessError, sudo_delete_file, self.make_file()
        )

    def test_can_delete_file_in_development(self):
        filename = get_data_path("/var/lib/maas/dhcpd.conf")
        with open(filename, "wb") as fd:
            fd.write(factory.make_bytes())
        sudo_delete_file(filename)
        self.assertThat(filename, Not(FileExists()))


def load_script(filename):
    """Load the Python script at `filename` into a new module."""
    modname = os.path.relpath(filename, root).replace(os.sep, ".")
    module = types.ModuleType(modname)
    with tokenize.open(filename) as fd:
        code = compile(fd.read(), filename, "exec", dont_inherit=True)
        exec(code, module.__dict__, module.__dict__)
    return module


class TestSudoWriteFileScript(MAASTestCase):
    """Tests for `scripts/maas-write-file`."""

    def setUp(self):
        super(TestSudoWriteFileScript, self).setUp()
        self.script_path = os.path.join(root, "scripts/maas-write-file")
        self.script = load_script(self.script_path)
        self.script.atomic_write = create_autospec(self.script.atomic_write)

    def test__white_list_is_a_non_empty_set_of_file_names(self):
        self.assertThat(self.script.whitelist, IsInstance(set))
        self.assertThat(self.script.whitelist, Not(HasLength(0)))
        self.assertThat(self.script.whitelist, AllMatch(IsInstance(str)))

    def test__accepts_file_names_on_white_list(self):
        calls_expected = []
        for filename in self.script.whitelist:
            content = factory.make_bytes()  # It's binary safe.
            mode = random.randint(0o000, 0o777)  # Inclusive of endpoints.
            args = self.script.arg_parser.parse_args([filename, oct(mode)])
            self.script.main(args, io.BytesIO(content))
            calls_expected.append(
                call(content, filename, overwrite=True, mode=mode)
            )
        self.assertThat(
            self.script.atomic_write, MockCallsMatch(*calls_expected)
        )

    def test__rejects_file_name_not_on_white_list(self):
        filename = factory.make_name("/some/where", sep="/")
        mode = random.randint(0o000, 0o777)  # Inclusive of endpoints.
        args = self.script.arg_parser.parse_args([filename, oct(mode)])
        with CaptureStandardIO() as stdio:
            error = self.assertRaises(
                SystemExit, self.script.main, args, io.BytesIO()
            )
        self.assertThat(error.code, GreaterThan(0))
        self.assertThat(self.script.atomic_write, MockNotCalled())
        self.assertThat(stdio.getOutput(), Equals(""))
        self.assertThat(
            stdio.getError(),
            DocTestMatches(
                "usage: ... Given filename ... is not in the "
                "white list. Choose from: ..."
            ),
        )

    def test__rejects_file_mode_with_high_bits_set(self):
        filename = random.choice(list(self.script.whitelist))
        mode = random.randint(0o1000, 0o7777)  # Inclusive of endpoints.
        args = self.script.arg_parser.parse_args([filename, oct(mode)])
        with CaptureStandardIO() as stdio:
            error = self.assertRaises(
                SystemExit, self.script.main, args, io.BytesIO()
            )
        self.assertThat(error.code, GreaterThan(0))
        self.assertThat(self.script.atomic_write, MockNotCalled())
        self.assertThat(stdio.getOutput(), Equals(""))
        self.assertThat(
            stdio.getError(),
            DocTestMatches(
                "usage: ... Given file mode 0o... is not permitted; "
                "only permission bits may be set."
            ),
        )


class TestSudoDeleteFileScript(MAASTestCase):
    """Tests for `scripts/maas-delete-file`."""

    def setUp(self):
        super(TestSudoDeleteFileScript, self).setUp()
        self.script_path = os.path.join(root, "scripts/maas-delete-file")
        self.script = load_script(self.script_path)
        self.script.atomic_delete = create_autospec(self.script.atomic_delete)

    def test__white_list_is_a_non_empty_set_of_file_names(self):
        self.assertThat(self.script.whitelist, IsInstance(set))
        self.assertThat(self.script.whitelist, Not(HasLength(0)))
        self.assertThat(self.script.whitelist, AllMatch(IsInstance(str)))

    def test__accepts_file_names_on_white_list(self):
        calls_expected = []
        for filename in self.script.whitelist:
            args = self.script.arg_parser.parse_args([filename])
            self.script.main(args)
            calls_expected.append(call(filename))
        self.assertThat(
            self.script.atomic_delete, MockCallsMatch(*calls_expected)
        )

    def test__is_okay_when_the_file_does_not_exist(self):
        filename = random.choice(list(self.script.whitelist))
        args = self.script.arg_parser.parse_args([filename])
        self.script.atomic_delete.side_effect = FileNotFoundError
        self.script.main(args)
        self.assertThat(
            self.script.atomic_delete, MockCalledOnceWith(filename)
        )

    def test__rejects_file_name_not_on_white_list(self):
        filename = factory.make_name("/some/where", sep="/")
        args = self.script.arg_parser.parse_args([filename])
        with CaptureStandardIO() as stdio:
            error = self.assertRaises(SystemExit, self.script.main, args)
        self.assertThat(error.code, GreaterThan(0))
        self.assertThat(self.script.atomic_delete, MockNotCalled())
        self.assertThat(stdio.getOutput(), Equals(""))
        self.assertThat(
            stdio.getError(),
            DocTestMatches(
                "usage: ... Given filename ... is not in the "
                "white list. Choose from: ..."
            ),
        )


class TestTempDir(MAASTestCase):
    def test_creates_real_fresh_directory(self):
        stored_text = factory.make_string()
        filename = factory.make_name("test-file")
        with tempdir() as directory:
            self.assertThat(directory, DirExists())
            write_text_file(os.path.join(directory, filename), stored_text)
            retrieved_text = read_text_file(os.path.join(directory, filename))
            files = os.listdir(directory)

        self.assertEqual(stored_text, retrieved_text)
        self.assertEqual([filename], files)

    def test_creates_unique_directory(self):
        with tempdir() as dir1, tempdir() as dir2:
            pass
        self.assertNotEqual(dir1, dir2)

    def test_cleans_up_on_successful_exit(self):
        with tempdir() as directory:
            file_path = factory.make_file(directory)

        self.assertThat(directory, Not(DirExists()))
        self.assertThat(file_path, Not(FileExists()))

    def test_cleans_up_on_exception_exit(self):
        class DeliberateFailure(Exception):
            pass

        with ExpectedException(DeliberateFailure):
            with tempdir() as directory:
                file_path = factory.make_file(directory)
                raise DeliberateFailure("Exiting context by exception")

        self.assertThat(directory, Not(DirExists()))
        self.assertThat(file_path, Not(FileExists()))

    def test_tolerates_disappearing_dir(self):
        with tempdir() as directory:
            rmtree(directory)

        self.assertThat(directory, Not(DirExists()))

    def test_uses_location(self):
        temp_location = self.make_dir()
        with tempdir(location=temp_location) as directory:
            self.assertThat(directory, DirExists())
            location_listing = os.listdir(temp_location)

        self.assertNotEqual(temp_location, directory)
        self.assertThat(directory, StartsWith(temp_location + os.path.sep))
        self.assertIn(os.path.basename(directory), location_listing)
        self.assertThat(temp_location, DirExists())
        self.assertThat(directory, Not(DirExists()))

    def test_yields_unicode(self):
        with tempdir() as directory:
            pass

        self.assertIsInstance(directory, str)

    def test_accepts_unicode_from_mkdtemp(self):
        fake_dir = os.path.join(self.make_dir(), factory.make_name("tempdir"))
        self.assertIsInstance(fake_dir, str)
        self.patch(tempfile, "mkdtemp").return_value = fake_dir

        with tempdir() as directory:
            pass

        self.assertEqual(fake_dir, directory)
        self.assertIsInstance(directory, str)

    def test_uses_prefix(self):
        prefix = factory.make_string(3)
        with tempdir(prefix=prefix) as directory:
            pass

        self.assertThat(os.path.basename(directory), StartsWith(prefix))

    def test_uses_suffix(self):
        suffix = factory.make_string(3)
        with tempdir(suffix=suffix) as directory:
            pass

        self.assertThat(os.path.basename(directory), EndsWith(suffix))

    def test_restricts_access(self):
        with tempdir() as directory:
            mode = os.stat(directory).st_mode
        self.assertEqual(
            stat.S_IMODE(mode), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        )


class TestReadTextFile(MAASTestCase):
    def test_reads_file(self):
        text = factory.make_string()
        self.assertEqual(text, read_text_file(self.make_file(contents=text)))

    def test_defaults_to_utf8(self):
        # Test input: "registered trademark" (ringed R) symbol.
        text = "\xae"
        self.assertEqual(
            text, read_text_file(self.make_file(contents=text.encode("utf-8")))
        )

    def test_uses_given_encoding(self):
        # Test input: "registered trademark" (ringed R) symbol.
        text = "\xae"
        self.assertEqual(
            text,
            read_text_file(
                self.make_file(contents=text.encode("utf-16")),
                encoding="utf-16",
            ),
        )


class TestWriteTextFile(MAASTestCase):
    def test_creates_file(self):
        path = os.path.join(self.make_dir(), factory.make_name("text"))
        text = factory.make_string()
        write_text_file(path, text)
        self.assertThat(path, FileContains(text, encoding="ascii"))

    def test_overwrites_file(self):
        path = self.make_file(contents="original text")
        text = factory.make_string()
        write_text_file(path, text)
        self.assertThat(path, FileContains(text, encoding="ascii"))

    def test_defaults_to_utf8(self):
        path = self.make_file()
        # Test input: "registered trademark" (ringed R) symbol.
        text = "\xae"
        write_text_file(path, text)
        with open(path, "r", encoding="utf-8") as fd:
            self.assertThat(fd.read(), Equals(text))

    def test_uses_given_encoding(self):
        path = self.make_file()
        # Test input: "registered trademark" (ringed R) symbol.
        text = "\xae"
        write_text_file(path, text, encoding="utf-16")
        with open(path, "r", encoding="utf-16") as fd:
            self.assertThat(fd.read(), Equals(text))


class TestSystemLocks(MAASTestCase):
    """Tests for `SystemLock` and its children."""

    scenarios = (
        ("FileLock", dict(locktype=FileLock)),
        ("RunLock", dict(locktype=RunLock)),
        ("SystemLock", dict(locktype=SystemLock)),
        ("NamedLock", dict(locktype=NamedLock)),
    )

    def make_lock(self):
        if self.locktype is NamedLock:
            lockname = factory.make_name("lock")
            return self.locktype(lockname)
        else:
            lockdir = self.make_dir()
            lockpath = os.path.join(lockdir, factory.make_name("lockfile"))
            return self.locktype(lockpath)

    def ensure_global_lock_held_when_locking_and_unlocking(self, lock):
        # Patch the lock to check that PROCESS_LOCK is held when doing IO.

        def do_lock():
            self.assertTrue(self.locktype.PROCESS_LOCK.locked())
            return True

        self.patch(lock._fslock, "lock").side_effect = do_lock

        def do_unlock():
            self.assertTrue(self.locktype.PROCESS_LOCK.locked())

        self.patch(lock._fslock, "unlock").side_effect = do_unlock

    def test__path_is_read_only(self):
        lock = self.make_lock()
        with ExpectedException(AttributeError):
            lock.path = factory.make_name()

    def test__holds_file_system_lock(self):
        lock = self.make_lock()
        self.assertFalse(lockfile.isLocked(lock.path))
        with lock:
            self.assertTrue(lockfile.isLocked(lock.path))
        self.assertFalse(lockfile.isLocked(lock.path))

    def test__is_locked_reports_accurately(self):
        lock = self.make_lock()
        self.assertFalse(lock.is_locked())
        with lock:
            self.assertTrue(lock.is_locked())
        self.assertFalse(lock.is_locked())

    def test__is_locked_holds_global_lock(self):
        lock = self.make_lock()
        PROCESS_LOCK = self.patch(self.locktype, "PROCESS_LOCK")
        self.assertFalse(lock.is_locked())
        self.assertThat(PROCESS_LOCK.__enter__, MockCalledOnceWith())
        self.assertThat(
            PROCESS_LOCK.__exit__, MockCalledOnceWith(None, None, None)
        )

    def test__cannot_be_acquired_twice(self):
        """
        `SystemLock` and its kin do not suffer from a bug that afflicts
        ``lockfile`` (https://pypi.python.org/pypi/lockfile):

          >>> from lockfile import FileLock
          >>> with FileLock('foo'):
          ...     with FileLock('foo'):
          ...         print("Hello!")
          ...
          Hello!
          Traceback (most recent call last):
            File "<stdin>", line 3, in <module>
            File ".../dist-packages/lockfile.py", line 230, in __exit__
              self.release()
            File ".../dist-packages/lockfile.py", line 271, in release
              raise NotLocked
          lockfile.NotLocked

        """
        lock = self.make_lock()
        with lock:
            with ExpectedException(self.locktype.NotAvailable, lock.path):
                with lock:
                    pass

    def test__locks_and_unlocks_while_holding_global_lock(self):
        lock = self.make_lock()
        self.ensure_global_lock_held_when_locking_and_unlocking(lock)

        with lock:
            self.assertFalse(self.locktype.PROCESS_LOCK.locked())

        self.assertThat(lock._fslock.lock, MockCalledOnceWith())
        self.assertThat(lock._fslock.unlock, MockCalledOnceWith())

    def test__wait_waits_until_lock_can_be_acquired(self):
        clock = self.patch(internet, "reactor", Clock())
        sleep = self.patch(fs_module, "sleep")
        sleep.side_effect = clock.advance

        lock = self.make_lock()
        do_lock = self.patch(lock._fslock, "lock")
        do_unlock = self.patch(lock._fslock, "unlock")

        do_lock.side_effect = [False, False, True]

        with lock.wait(10):
            self.assertThat(do_lock, MockCallsMatch(call(), call(), call()))
            self.assertThat(sleep, MockCallsMatch(call(1.0), call(1.0)))
            self.assertThat(do_unlock, MockNotCalled())

        self.assertThat(do_unlock, MockCalledOnceWith())

    def test__wait_raises_exception_when_time_has_run_out(self):
        clock = self.patch(internet, "reactor", Clock())
        sleep = self.patch(fs_module, "sleep")
        sleep.side_effect = clock.advance

        lock = self.make_lock()
        do_lock = self.patch(lock._fslock, "lock")
        do_unlock = self.patch(lock._fslock, "unlock")

        do_lock.return_value = False

        with ExpectedException(self.locktype.NotAvailable):
            with lock.wait(0.2):
                pass

        self.assertThat(do_lock, MockCallsMatch(call(), call(), call()))
        self.assertThat(sleep, MockCallsMatch(call(0.1), call(0.1)))
        self.assertThat(do_unlock, MockNotCalled())

    def test__wait_locks_and_unlocks_while_holding_global_lock(self):
        lock = self.make_lock()
        self.ensure_global_lock_held_when_locking_and_unlocking(lock)

        with lock.wait(10):
            self.assertFalse(self.locktype.PROCESS_LOCK.locked())

        self.assertThat(lock._fslock.lock, MockCalledOnceWith())
        self.assertThat(lock._fslock.unlock, MockCalledOnceWith())

    def test__context_is_implemented_using_acquire_and_release(self):
        # Thus implying that all the earlier tests are valid for both.
        lock = self.make_lock()
        acquire = self.patch(lock, "acquire")
        release = self.patch(lock, "release")

        self.assertThat(acquire, MockNotCalled())
        self.assertThat(release, MockNotCalled())
        # Not locked at first, naturally.
        self.assertFalse(lock.is_locked())
        lock.acquire()
        try:
            self.assertThat(acquire, MockCalledOnceWith())
            self.assertThat(release, MockNotCalled())
            # The lock is not locked because — ah ha! — we've patched out the
            # method that does the locking.
            self.assertFalse(lock.is_locked())
        finally:
            lock.release()
        self.assertThat(acquire, MockCalledOnceWith())
        self.assertThat(release, MockCalledOnceWith())
        # Still not locked, without surprise.
        self.assertFalse(lock.is_locked())


class TestSystemLock(MAASTestCase):
    """Tests specific to `SystemLock`."""

    def test__path(self):
        filename = self.make_file()
        observed = SystemLock(filename).path
        self.assertEqual(filename, observed)


class TestFileLock(MAASTestCase):
    """Tests specific to `FileLock`."""

    def test__path(self):
        filename = self.make_file()
        expected = filename + ".lock"
        observed = FileLock(filename).path
        self.assertEqual(expected, observed)


class TestRunLock(MAASTestCase):
    """Tests specific to `RunLock`."""

    def test__string_path(self):
        filename = "/foo/bar/123:456.txt"
        expected = get_tentative_data_path(
            "/run/lock/maas@foo:bar:123::456.txt"
        )
        observed = RunLock(filename).path
        self.assertEqual(expected, observed)

    def test__byte_path(self):
        filename = b"/foo/bar/123:456.txt"
        expected = get_tentative_data_path(
            "/run/lock/maas@foo:bar:123::456.txt"
        )
        observed = RunLock(filename).path
        self.assertEqual(expected, observed)


class TestNamedLock(MAASTestCase):
    """Tests specific to `NamedLock`."""

    def test__string_name(self):
        name = factory.make_name("lock")
        expected = get_tentative_data_path("/run/lock/maas:" + name)
        observed = NamedLock(name).path
        self.assertEqual(expected, observed)

    def test__byte_name_is_rejected(self):
        name = factory.make_name("lock").encode("ascii")
        error = self.assertRaises(TypeError, NamedLock, name)
        self.assertThat(str(error), Equals("Lock name must be str, not bytes"))

    def test__name_rejects_unacceptable_characters(self):
        # This demonstrates that validation is performed, but it is not an
        # exhaustive test by any means.
        self.assertRaises(ValueError, NamedLock, "foo:bar")
        self.assertRaises(ValueError, NamedLock, "foo^bar")
        self.assertRaises(ValueError, NamedLock, "(foobar)")
        self.assertRaises(ValueError, NamedLock, "foo*bar")
        self.assertRaises(ValueError, NamedLock, "foo/bar")
        self.assertRaises(ValueError, NamedLock, "foo=bar")
        # The error message contains all of the unacceptable characters.
        error = self.assertRaises(ValueError, NamedLock, "[foo;bar]")
        self.assertThat(
            str(error), Equals("Lock name contains illegal characters: ;[]")
        )
