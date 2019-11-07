# Copyright 2014-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.boot.uefi_amd64`."""

__all__ = []

import os
import re
from unittest.mock import sentinel

from maastesting.factory import factory
from maastesting.matchers import FileContains, MockAnyCall, MockCalledOnce
from maastesting.testcase import MAASTestCase
from provisioningserver.boot import (
    BytesReader,
    uefi_amd64 as uefi_amd64_module,
)
from provisioningserver.boot.testing import TFTPPath, TFTPPathAndComponents
from provisioningserver.boot.tftppath import compose_image_path
from provisioningserver.boot.uefi_amd64 import (
    CONFIG_FILE,
    re_config_file,
    UEFIAMD64BootMethod,
    UEFIAMD64HTTPBootMethod,
)
from provisioningserver.tests.test_kernel_opts import make_kernel_parameters
from provisioningserver.utils import typed
from provisioningserver.utils.fs import tempdir
from provisioningserver.utils.network import convert_host_to_uri_str
from testtools.matchers import (
    ContainsAll,
    IsInstance,
    MatchesAll,
    MatchesRegex,
    StartsWith,
)


@typed
def compose_config_path(
    mac: str = None, arch: str = None, subarch: str = None
) -> TFTPPath:
    """Compose the TFTP path for a UEFI configuration file.

    The path returned is relative to the TFTP root, as it would be
    identified by clients on the network.

    :param mac: A MAC address, in IEEE 802 colon-separated form,
        corresponding to the machine for which this configuration is
        relevant.
    :param arch: Architecture for the booting machine, for UEFI this is
        always amd64.
    :param subarch: Sub-architecture type, this is normally always generic.
    :return: Path for the corresponding PXE config file as exposed over
        TFTP, as a byte string.
    """
    if mac is not None:
        return "grub/grub.cfg-{mac}".format(mac=mac).encode("ascii")
    if arch is not None:
        if subarch is None:
            subarch = "generic"
        return "grub/grub.cfg-{arch}-{subarch}".format(
            arch=arch, subarch=subarch
        ).encode("ascii")
    return "grub/grub.cfg".encode("ascii")


class TestUEFIAMD64BootMethodRender(MAASTestCase):
    """Tests for
    `provisioningserver.boot_amd64.uefi.UEFIAMD64BootMethod.render`."""

    def test_get_reader(self):
        # Given the right configuration options, the UEFI configuration is
        # correctly rendered.
        method = UEFIAMD64BootMethod()
        params = make_kernel_parameters(arch="amd64", purpose="xinstall")
        fs_host = "(http,%s:5248)/images" % (
            convert_host_to_uri_str(params.fs_host)
        )
        output = method.get_reader(backend=None, kernel_params=params)
        # The output is a BytesReader.
        self.assertThat(output, IsInstance(BytesReader))
        output = output.read(10000).decode("utf-8")
        # The template has rendered without error. UEFI configurations
        # typically start with a DEFAULT line.
        self.assertThat(output, StartsWith('set default="0"'))
        # The UEFI parameters are all set according to the options.
        image_dir = compose_image_path(
            osystem=params.osystem,
            arch=params.arch,
            subarch=params.subarch,
            release=params.release,
            label=params.label,
        )

        self.assertThat(
            output,
            MatchesAll(
                MatchesRegex(
                    r".*\s+lin.*cc:\\{\'datasource_list\':"
                    r" \[\'MAAS\'\]\\}end_cc.*",
                    re.MULTILINE | re.DOTALL,
                ),
                MatchesRegex(
                    r".*^\s+linuxefi  %s/%s/%s .+?$"
                    % (
                        re.escape(fs_host),
                        re.escape(image_dir),
                        params.kernel,
                    ),
                    re.MULTILINE | re.DOTALL,
                ),
                MatchesRegex(
                    r".*^\s+initrdefi %s/%s/%s$"
                    % (
                        re.escape(fs_host),
                        re.escape(image_dir),
                        params.initrd,
                    ),
                    re.MULTILINE | re.DOTALL,
                ),
            ),
        )

    def test_get_reader_with_extra_arguments_does_not_affect_output(self):
        # get_reader() allows any keyword arguments as a safety valve.
        method = UEFIAMD64BootMethod()
        options = {
            "backend": None,
            "kernel_params": make_kernel_parameters(purpose="install"),
        }
        # Capture the output before sprinking in some random options.
        output_before = method.get_reader(**options).read(10000)
        # Sprinkle some magic in.
        options.update(
            (factory.make_name("name"), factory.make_name("value"))
            for _ in range(10)
        )
        # Capture the output after sprinking in some random options.
        output_after = method.get_reader(**options).read(10000)
        # The generated template is the same.
        self.assertEqual(output_before, output_after)

    def test_get_reader_with_local_purpose(self):
        # If purpose is "local", the config.localboot.template should be
        # used.
        method = UEFIAMD64BootMethod()
        options = {
            "backend": None,
            "kernel_params": make_kernel_parameters(
                purpose="local", arch="amd64"
            ),
        }
        output = method.get_reader(**options).read(10000).decode("utf-8")
        self.assertIn("chainloader /efi/", output)
        self.assertIn("bootx64.efi", output)
        self.assertIn("shimx64.efi", output)
        self.assertIn("grubx64.efi", output)

    def test_get_reader_with_enlist_purpose(self):
        # If purpose is "enlist", the config.enlist.template should be
        # used.
        method = UEFIAMD64BootMethod()
        params = make_kernel_parameters(purpose="enlist", arch="amd64")
        options = {"backend": None, "kernel_params": params}
        output = method.get_reader(**options).read(10000).decode("utf-8")
        self.assertThat(
            output,
            ContainsAll(
                [
                    "menuentry 'Enlist'",
                    "%s/%s/%s" % (params.osystem, params.arch, params.subarch),
                    params.kernel,
                ]
            ),
        )

    def test_get_reader_with_commissioning_purpose(self):
        # If purpose is "commissioning", the config.commissioning.template
        # should be used.
        method = UEFIAMD64BootMethod()
        params = make_kernel_parameters(purpose="commissioning", arch="amd64")
        options = {"backend": None, "kernel_params": params}
        output = method.get_reader(**options).read(10000).decode("utf-8")
        self.assertThat(
            output,
            ContainsAll(
                [
                    "menuentry 'Commission'",
                    "%s/%s/%s" % (params.osystem, params.arch, params.subarch),
                    params.kernel,
                ]
            ),
        )


class TestUEFIAMD64BootMethodRegex(MAASTestCase):
    """Tests
    `provisioningserver.boot.uefi_amd64.UEFIAMD64BootMethod.re_config_file`."""

    @staticmethod
    @typed
    def get_example_path_and_components() -> TFTPPathAndComponents:
        """Return a plausible UEFI path and its components.

        The path is intended to match `re_config_file`, and
        the components are the expected groups from a match.
        """
        mac = factory.make_mac_address(":")
        return (
            compose_config_path(mac),
            {"mac": mac.encode("ascii"), "arch": None, "subarch": None},
        )

    def test_re_config_file_is_compatible_with_cfg_path_generator(self):
        # The regular expression for extracting components of the file path is
        # compatible with the PXE config path generator.
        for iteration in range(10):
            config_path, args = self.get_example_path_and_components()
            match = re_config_file.match(config_path)
            self.assertIsNotNone(match, config_path)
            self.assertEqual(args, match.groupdict())

    def test_re_config_file_with_leading_slash(self):
        # The regular expression for extracting components of the file path
        # doesn't care if there's a leading forward slash; the TFTP server is
        # easy on this point, so it makes sense to be also.
        config_path, args = self.get_example_path_and_components()
        # Ensure there's a leading slash.
        config_path = b"/" + config_path.lstrip(b"/")
        match = re_config_file.match(config_path)
        self.assertIsNotNone(match, config_path)
        self.assertEqual(args, match.groupdict())

    def test_re_config_file_without_leading_slash(self):
        # The regular expression for extracting components of the file path
        # doesn't care if there's no leading forward slash; the TFTP server is
        # easy on this point, so it makes sense to be also.
        config_path, args = self.get_example_path_and_components()
        # Ensure there's no leading slash.
        config_path = config_path.lstrip(b"/")
        match = re_config_file.match(config_path)
        self.assertIsNotNone(match, config_path)
        self.assertEqual(args, match.groupdict())

    def test_re_config_file_matches_classic_grub_cfg(self):
        # The default config path is simply "grub.cfg-{mac}" (without
        # leading slash).  The regex matches this.
        mac = b"aa:bb:cc:dd:ee:ff"
        match = re_config_file.match(b"grub/grub.cfg-%s" % mac)
        self.assertIsNotNone(match)
        self.assertEqual(
            {"mac": mac, "arch": None, "subarch": None}, match.groupdict()
        )

    def test_re_config_file_matches_grub_cfg_with_leading_slash(self):
        mac = b"aa:bb:cc:dd:ee:ff"
        match = re_config_file.match(b"/grub/grub.cfg-%s" % mac)
        self.assertIsNotNone(match)
        self.assertEqual(
            {"mac": mac, "arch": None, "subarch": None}, match.groupdict()
        )

    def test_re_config_file_does_not_match_default_grub_config_file(self):
        self.assertIsNone(re_config_file.match(b"grub/grub.cfg"))

    def test_re_config_file_with_default(self):
        match = re_config_file.match(b"grub/grub.cfg-default")
        self.assertIsNotNone(match)
        self.assertEqual(
            {"mac": None, "arch": None, "subarch": None}, match.groupdict()
        )

    def test_re_config_file_with_default_arch(self):
        arch = factory.make_name("arch", sep="").encode("ascii")
        match = re_config_file.match(b"grub/grub.cfg-default-%s" % arch)
        self.assertIsNotNone(match)
        self.assertEqual(
            {"mac": None, "arch": arch, "subarch": None}, match.groupdict()
        )

    def test_re_config_file_with_default_arch_and_subarch(self):
        arch = factory.make_name("arch", sep="").encode("ascii")
        subarch = factory.make_name("subarch", sep="").encode("ascii")
        match = re_config_file.match(
            b"grub/grub.cfg-default-%s-%s" % (arch, subarch)
        )
        self.assertIsNotNone(match)
        self.assertEqual(
            {"mac": None, "arch": arch, "subarch": subarch}, match.groupdict()
        )


class TestUEFIAMD64BootMethod(MAASTestCase):
    """Tests `provisioningserver.boot.uefi_amd64.UEFIAMD64BootMethod`."""

    def test_link_bootloader_creates_grub_cfg(self):
        method = UEFIAMD64BootMethod()
        with tempdir() as tmp:
            stream_path = os.path.join(
                tmp,
                "bootloader",
                method.bios_boot_method,
                method.bootloader_arches[0],
            )
            os.makedirs(stream_path)
            for bootloader_file in method.bootloader_files:
                factory.make_file(stream_path, bootloader_file)

            method.link_bootloader(tmp)

            for bootloader_file in method.bootloader_files:
                bootloader_file_path = os.path.join(tmp, bootloader_file)
                self.assertTrue(os.path.islink(bootloader_file_path))
            grub_file_path = os.path.join(tmp, "grub", "grub.cfg")
            self.assertTrue(grub_file_path, FileContains(CONFIG_FILE))

    def test_link_bootloader_copies_previous_downloaded_files(self):
        method = UEFIAMD64BootMethod()
        with tempdir() as tmp:
            new_dir = os.path.join(tmp, "new")
            current_dir = os.path.join(tmp, "current")
            os.makedirs(new_dir)
            os.makedirs(current_dir)
            for bootloader_file in method.bootloader_files:
                factory.make_file(current_dir, bootloader_file)

            method.link_bootloader(new_dir)

            for bootloader_file in method.bootloader_files:
                bootloader_file_path = os.path.join(new_dir, bootloader_file)
                self.assertTrue(os.path.isfile(bootloader_file_path))

    def test_link_bootloader_copies_from_system(self):
        method = UEFIAMD64BootMethod()
        bootloader_dir = "/var/lib/maas/boot-resources/%s" % factory.make_name(
            "snapshot"
        )
        # Since the fall back looks for paths on the filesystem we need to
        # intercept the calls and make sure they were called with the right
        # arguments otherwise the test environment will interfere.
        allowed_src_files = [
            "/usr/lib/shim/shim.efi.signed",
            "/usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed",
        ]

        def fake_exists(path):
            if path in allowed_src_files:
                return True
            else:
                return False

        self.patch(
            uefi_amd64_module.os.path, "exists"
        ).side_effect = fake_exists
        mock_atomic_symlink = self.patch(uefi_amd64_module, "atomic_symlink")

        method._find_and_copy_bootloaders(bootloader_dir)

        self.assertThat(
            mock_atomic_symlink,
            MockAnyCall(
                "/usr/lib/shim/shim.efi.signed",
                os.path.join(bootloader_dir, "bootx64.efi"),
            ),
        )
        self.assertThat(
            mock_atomic_symlink,
            MockAnyCall(
                "/usr/lib/grub/x86_64-efi-signed/grubnetx64.efi.signed",
                os.path.join(bootloader_dir, "grubx64.efi"),
            ),
        )

    def test_link_bootloader_logs_missing_bootloader_files(self):
        method = UEFIAMD64BootMethod()
        self.patch(uefi_amd64_module.os.path, "exists").return_value = False
        mock_maaslog = self.patch(uefi_amd64_module.maaslog, "error")
        bootloader_dir = "/var/lib/maas/boot-resources/%s" % factory.make_name(
            "snapshot"
        )
        method._find_and_copy_bootloaders(bootloader_dir)
        self.assertThat(mock_maaslog, MockCalledOnce())


class TestUEFIAMD64HTTPBootMethod(MAASTestCase):
    """Tests `provisioningserver.boot.uefi_amd64.UEFIAMD64HTTPBootMethod`."""

    def test_attributes(self):
        method = UEFIAMD64HTTPBootMethod()
        self.assertEqual("uefi_amd64_http", method.name)
        self.assertEqual("uefi", method.bios_boot_method)
        self.assertEqual("uefi", method.template_subdir)
        self.assertEqual("bootx64.efi", method.bootloader_path)
        self.assertEqual([], method.bootloader_arches)
        self.assertEqual([], method.bootloader_files)
        self.assertEqual(["00:0f", "00:10"], method.arch_octet)
        self.assertIsNone(method.user_class)
        self.assertTrue(method.absolute_url_as_filename)
        self.assertTrue(method.http_url)

    def test_match_path_returns_None(self):
        method = UEFIAMD64HTTPBootMethod()
        self.assertIsNone(method.match_path(sentinel.backend, sentinel.path))

    def test_get_reader_returns_None(self):
        method = UEFIAMD64HTTPBootMethod()
        self.assertIsNone(method.get_reader(sentinel.backend, sentinel.params))

    def test_link_bootloader_returns_None(self):
        method = UEFIAMD64HTTPBootMethod()
        self.assertIsNone(method.link_bootloader(sentinel.destination))
