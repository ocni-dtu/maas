# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.boot.powernv`."""

__all__ = []

import re
from unittest.mock import Mock

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver.boot import BytesReader, powernv as powernv_module
from provisioningserver.boot.powernv import (
    ARP_HTYPE,
    format_bootif,
    PowerNVBootMethod,
    re_config_file,
)
from provisioningserver.boot.testing import TFTPPathAndComponents
from provisioningserver.boot.tests.test_pxe import parse_pxe_config
from provisioningserver.boot.tftppath import compose_image_path
from provisioningserver.testing.config import ClusterConfigurationFixture
from provisioningserver.tests.test_kernel_opts import make_kernel_parameters
from provisioningserver.utils import typed
from provisioningserver.utils.network import convert_host_to_uri_str
from testtools.matchers import (
    IsInstance,
    MatchesAll,
    MatchesRegex,
    Not,
    StartsWith,
)
from twisted.python.filepath import FilePath


@typed
def compose_config_path(mac: str) -> bytes:
    """Compose the TFTP path for a PowerNV PXE configuration file.

    The path returned is relative to the TFTP root, as it would be
    identified by clients on the network.

    :param mac: A MAC address, in IEEE 802 hyphen-separated form,
        corresponding to the machine for which this configuration is
        relevant. This relates to PXELINUX's lookup protocol.
    :return: Path for the corresponding PXE config file as exposed over
        TFTP, as a byte string.
    """
    # Not using os.path.join: this is a TFTP path, not a native path. Yes, in
    # practice for us they're the same. We always assume that the ARP HTYPE
    # (hardware type) that PXELINUX sends is Ethernet.
    return "ppc64el/pxelinux.cfg/{htype:02x}-{mac}".format(
        htype=ARP_HTYPE.ETHERNET, mac=mac
    ).encode("ascii")


@typed
def get_example_path_and_components() -> TFTPPathAndComponents:
    """Return a plausible path and its components.

    The path is intended to match `re_config_file`, and the components are
    the expected groups from a match.
    """
    mac = factory.make_mac_address("-")
    return compose_config_path(mac), {"mac": mac.encode("ascii")}


class TestPowerNVBootMethod(MAASTestCase):
    def make_tftp_root(self):
        """Set, and return, a temporary TFTP root directory."""
        tftproot = self.make_dir()
        self.useFixture(ClusterConfigurationFixture(tftp_root=tftproot))
        return FilePath(tftproot)

    def test_compose_config_path_follows_maas_pxe_directory_layout(self):
        mac = factory.make_mac_address("-")
        self.assertEqual(
            "ppc64el/pxelinux.cfg/%02x-%s" % (ARP_HTYPE.ETHERNET, mac),
            compose_config_path(mac).decode("ascii"),
        )

    def test_compose_config_path_does_not_include_tftp_root(self):
        tftproot = self.make_tftp_root().asBytesMode()
        mac = factory.make_mac_address("-")
        self.assertThat(
            compose_config_path(mac), Not(StartsWith(tftproot.path))
        )

    def test_bootloader_path(self):
        method = PowerNVBootMethod()
        self.assertEqual("pxelinux.0", method.bootloader_path)

    def test_bootloader_path_does_not_include_tftp_root(self):
        tftproot = self.make_tftp_root()
        method = PowerNVBootMethod()
        self.assertThat(method.bootloader_path, Not(StartsWith(tftproot.path)))

    def test_name(self):
        method = PowerNVBootMethod()
        self.assertEqual("powernv", method.name)

    def test_template_subdir(self):
        method = PowerNVBootMethod()
        self.assertEqual("pxe", method.template_subdir)

    def test_arch_octet(self):
        method = PowerNVBootMethod()
        self.assertEqual("00:0E", method.arch_octet)

    def test_path_prefix(self):
        method = PowerNVBootMethod()
        self.assertEqual("ppc64el/", method.path_prefix)


class TestPowerNVBootMethodMatchPath(MAASTestCase):
    """Tests for
    `provisioningserver.boot.powernv.PowerNVBootMethod.match_path`.
    """

    def test_match_path_pxe_config_with_mac(self):
        method = PowerNVBootMethod()
        config_path, args = get_example_path_and_components()
        params = method.match_path(None, config_path)
        expected = {"arch": "ppc64el", "mac": args["mac"].decode("ascii")}
        self.assertEqual(expected, params)

    def test_match_path_pxe_config_without_mac(self):
        method = PowerNVBootMethod()
        fake_mac = factory.make_mac_address("-")
        self.patch(powernv_module, "get_remote_mac").return_value = fake_mac
        config_path = b"ppc64el/pxelinux.cfg/default"
        params = method.match_path(None, config_path)
        expected = {"arch": "ppc64el", "mac": fake_mac}
        self.assertEqual(expected, params)

    def test_match_path_pxe_prefix_request(self):
        method = PowerNVBootMethod()
        fake_mac = factory.make_mac_address("-")
        self.patch(powernv_module, "get_remote_mac").return_value = fake_mac
        file_path = b"ppc64el/file"
        params = method.match_path(None, file_path)
        expected = {
            "arch": "ppc64el",
            "mac": fake_mac,
            # The "ppc64el/" prefix has been removed from the path.
            "path": file_path.decode("utf-8")[8:],
        }
        self.assertEqual(expected, params)


class TestPowerNVBootMethodRenderConfig(MAASTestCase):
    """Tests for
    `provisioningserver.boot.powernv.PowerNVBootMethod.get_reader`
    """

    def test_get_reader_install(self):
        # Given the right configuration options, the PXE configuration is
        # correctly rendered.
        method = PowerNVBootMethod()
        params = make_kernel_parameters(
            self, arch="ppc64el", purpose="xinstall"
        )
        fs_host = "http://%s:5248/images" % (
            convert_host_to_uri_str(params.fs_host)
        )
        output = method.get_reader(backend=None, kernel_params=params)
        # The output is a BytesReader.
        self.assertThat(output, IsInstance(BytesReader))
        output = output.read(10000).decode("utf-8")
        # The template has rendered without error. PXELINUX configurations
        # typically start with a DEFAULT line.
        self.assertThat(output, StartsWith("DEFAULT "))
        # The PXE parameters are all set according to the options.
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
                    r".*^\s+KERNEL %s/%s/%s$"
                    % (
                        re.escape(fs_host),
                        re.escape(image_dir),
                        params.kernel,
                    ),
                    re.MULTILINE | re.DOTALL,
                ),
                MatchesRegex(
                    r".*^\s+INITRD %s/%s/%s$"
                    % (
                        re.escape(fs_host),
                        re.escape(image_dir),
                        params.initrd,
                    ),
                    re.MULTILINE | re.DOTALL,
                ),
                MatchesRegex(r".*^\s+APPEND .+?$", re.MULTILINE | re.DOTALL),
            ),
        )

    def test_get_reader_with_extra_arguments_does_not_affect_output(self):
        # get_reader() allows any keyword arguments as a safety valve.
        method = PowerNVBootMethod()
        options = {
            "backend": None,
            "kernel_params": make_kernel_parameters(
                self, arch="ppc64el", purpose="install"
            ),
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
        # If purpose is "local", output should be empty string.
        method = PowerNVBootMethod()
        options = {
            "backend": None,
            "kernel_params": make_kernel_parameters(
                arch="ppc64el", purpose="local"
            ),
        }
        output = method.get_reader(**options).read(10000).decode("utf-8")
        self.assertIn("", output)

    def test_get_reader_appends_bootif(self):
        method = PowerNVBootMethod()
        fake_mac = factory.make_mac_address("-")
        params = make_kernel_parameters(self, purpose="install")
        output = method.get_reader(
            backend=None, kernel_params=params, arch="ppc64el", mac=fake_mac
        )
        output = output.read(10000).decode("utf-8")
        config = parse_pxe_config(output)
        expected = "BOOTIF=%s" % format_bootif(fake_mac)
        self.assertIn(expected, config["execute"]["APPEND"])

    def test_format_bootif_replaces_colon(self):
        fake_mac = factory.make_mac_address("-")
        self.assertEqual(
            "01-%s" % fake_mac.replace(":", "-").lower(),
            format_bootif(fake_mac),
        )

    def test_format_bootif_makes_mac_address_lower(self):
        fake_mac = factory.make_mac_address("-")
        fake_mac = fake_mac.upper()
        self.assertEqual(
            "01-%s" % fake_mac.replace(":", "-").lower(),
            format_bootif(fake_mac),
        )


class TestPowerNVBootMethodPathPrefix(MAASTestCase):
    """Tests for `provisioningserver.boot.powernv.PowerNVBootMethod`."""

    def test_path_prefix_removed(self):
        temp_dir = FilePath(self.make_dir())
        backend = Mock(base=temp_dir)  # A `TFTPBackend`.

        # Create a file in the backend's base directory.
        data = factory.make_string().encode("ascii")
        temp_file = temp_dir.child("example")
        temp_file.setContent(data)

        method = PowerNVBootMethod()
        params = method.get_params(backend, b"ppc64el/example")
        self.assertEqual({"path": "example"}, params)
        reader = method.get_reader(backend, make_kernel_parameters(), **params)
        self.addCleanup(reader.finish)
        self.assertEqual(len(data), reader.size)
        self.assertEqual(data, reader.read(len(data)))
        self.assertEqual(b"", reader.read(1))

    def test_path_prefix_only_first_occurrence_removed(self):
        temp_dir = FilePath(self.make_dir())
        backend = Mock(base=temp_dir)  # A `TFTPBackend`.

        # Create a file nested within a "ppc64el" directory.
        data = factory.make_string().encode("ascii")
        temp_subdir = temp_dir.child("ppc64el")
        temp_subdir.createDirectory()
        temp_file = temp_subdir.child("example")
        temp_file.setContent(data)

        method = PowerNVBootMethod()
        params = method.get_params(backend, b"ppc64el/ppc64el/example")
        self.assertEqual({"path": "ppc64el/example"}, params)
        reader = method.get_reader(backend, make_kernel_parameters(), **params)
        self.addCleanup(reader.finish)
        self.assertEqual(len(data), reader.size)
        self.assertEqual(data, reader.read(len(data)))
        self.assertEqual(b"", reader.read(1))


class TestPowerNVBootMethodRegex(MAASTestCase):
    """Tests for
    `provisioningserver.boot.powernv.PowerNVBootMethod.re_config_file`.
    """

    def test_re_config_file_is_compatible_with_config_path_generator(self):
        # The regular expression for extracting components of the file path is
        # compatible with the PXE config path generator.
        for iteration in range(10):
            config_path, args = get_example_path_and_components()
            match = re_config_file.match(config_path)
            self.assertIsNotNone(match, config_path)
            self.assertEqual(args, match.groupdict())

    def test_re_config_file_with_leading_slash(self):
        # The regular expression for extracting components of the file path
        # doesn't care if there's a leading forward slash; the TFTP server is
        # easy on this point, so it makes sense to be also.
        config_path, args = get_example_path_and_components()
        # Ensure there's a leading slash.
        config_path = b"/" + config_path.lstrip(b"/")
        match = re_config_file.match(config_path)
        self.assertIsNotNone(match, config_path)
        self.assertEqual(args, match.groupdict())

    def test_re_config_file_without_leading_slash(self):
        # The regular expression for extracting components of the file path
        # doesn't care if there's no leading forward slash; the TFTP server is
        # easy on this point, so it makes sense to be also.
        config_path, args = get_example_path_and_components()
        # Ensure there's no leading slash.
        config_path = config_path.lstrip(b"/")
        match = re_config_file.match(config_path)
        self.assertIsNotNone(match, config_path)
        self.assertEqual(args, match.groupdict())

    def test_re_config_file_matches_classic_pxelinux_cfg(self):
        # The default config path is simply "pxelinux.cfg" (without
        # leading slash).  The regex matches this.
        mac = factory.make_mac_address("-").encode("ascii")
        match = re_config_file.match(b"ppc64el/pxelinux.cfg/01-%s" % mac)
        self.assertIsNotNone(match)
        self.assertEqual({"mac": mac}, match.groupdict())

    def test_re_config_file_matches_pxelinux_cfg_with_leading_slash(self):
        mac = factory.make_mac_address("-").encode("ascii")
        match = re_config_file.match(b"/ppc64el/pxelinux.cfg/01-%s" % mac)
        self.assertIsNotNone(match)
        self.assertEqual({"mac": mac}, match.groupdict())

    def test_re_config_file_does_not_match_non_config_file(self):
        self.assertIsNone(re_config_file.match(b"ppc64el/pxelinux.cfg/kernel"))

    def test_re_config_file_does_not_match_file_in_root(self):
        self.assertIsNone(re_config_file.match(b"01-aa-bb-cc-dd-ee-ff"))

    def test_re_config_file_does_not_match_file_not_in_pxelinux_cfg(self):
        self.assertIsNone(re_config_file.match(b"foo/01-aa-bb-cc-dd-ee-ff"))

    def test_re_config_file_with_default(self):
        match = re_config_file.match(b"ppc64el/pxelinux.cfg/default")
        self.assertIsNotNone(match)
        self.assertEqual({"mac": None}, match.groupdict())
