# Copyright 2012-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test composition of kernel command lines."""

__all__ = ["make_kernel_parameters"]

import os
import random
from unittest.mock import sentinel

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from provisioningserver import kernel_opts
from provisioningserver.drivers import Architecture, ArchitectureRegistry
from provisioningserver.kernel_opts import (
    compose_arch_opts,
    compose_kernel_command_line,
    CURTIN_KERNEL_CMDLINE_NAME,
    get_curtin_kernel_cmdline_sep,
    get_last_directory,
    KernelParameters,
)
from testtools.matchers import Contains, ContainsAll, Not


def make_kernel_parameters(testcase=None, **parms):
    """Make a randomly populated `KernelParameters` instance.

    If testcase is passed, we poke the generated arch/subarch into the
    ArchitectureRegistry and call addCleanup on the testcase to make sure
    it is removed after the test completes.
    """
    # fs_host needs to be an IP address, set it if it was not passed.
    if "fs_host" not in parms:
        parms.update({"fs_host": factory.make_ip_address()})
    had_log_port = "log_port" in parms
    parms.update(
        {
            field: factory.make_name(field)
            for field in KernelParameters._fields
            if field not in parms
        }
    )
    # KernelParameters will handle setting the default.
    if not had_log_port:
        del parms["log_port"]
    params = KernelParameters(**parms)

    if testcase is not None:
        name = "%s/%s" % (params.arch, params.subarch)
        if name in ArchitectureRegistry:
            # It's already there, no need to patch and risk overwriting
            # preset kernel options.
            return params
        resource = Architecture(name, name)
        ArchitectureRegistry.register_item(name, resource)

        testcase.addCleanup(ArchitectureRegistry.unregister_item, name)

    return params


class TestUtilitiesKernelOpts(MAASTestCase):
    def test_get_last_directory(self):
        root = self.make_dir()
        dir1 = os.path.join(root, "20120405")
        dir2 = os.path.join(root, "20120105")
        dir3 = os.path.join(root, "20120403")
        os.makedirs(dir1)
        os.makedirs(dir2)
        os.makedirs(dir3)
        self.assertEqual(dir1, get_last_directory(root))

    def test_kernel_parameters_callable(self):
        # KernelParameters instances are callable; an alias for _replace().
        params = make_kernel_parameters()
        self.assertTrue(callable(params))
        self.assertIs(params._replace.__func__, params.__call__.__func__)


class TestGetCurtinKernelCmdlineSepTest(MAASTestCase):
    def test_get_curtin_kernel_cmdline_sep_returns_curtin_value(self):
        sep = factory.make_name("separator")
        self.patch(kernel_opts.curtin, CURTIN_KERNEL_CMDLINE_NAME, sep)
        self.assertEqual(sep, get_curtin_kernel_cmdline_sep())

    def test_get_curtin_kernel_cmdline_sep_returns_default(self):
        original_sep = getattr(
            kernel_opts.curtin, CURTIN_KERNEL_CMDLINE_NAME, sentinel.missing
        )

        if original_sep != sentinel.missing:

            def restore_sep():
                setattr(
                    kernel_opts.curtin,
                    CURTIN_KERNEL_CMDLINE_NAME,
                    original_sep,
                )

            self.addCleanup(restore_sep)
            delattr(kernel_opts.curtin, CURTIN_KERNEL_CMDLINE_NAME)
        self.assertEqual("--", get_curtin_kernel_cmdline_sep())


class TestKernelOpts(MAASTestCase):
    def make_kernel_parameters(self, *args, **kwargs):
        return make_kernel_parameters(self, *args, **kwargs)

    def test_log_port_sets_default(self):
        params = self.make_kernel_parameters()
        self.assertEqual(5247, params.log_port)

    def test_log_port_overrides_default(self):
        new_port = factory.pick_port()
        params = self.make_kernel_parameters(log_port=new_port)
        self.assertEqual(new_port, params.log_port)

    def test_compose_kernel_command_line_includes_disable_overlay_cfg(self):
        params = self.make_kernel_parameters(
            purpose=random.choice(["commissioning", "xinstall", "enlist"])
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, Contains("overlayroot_cfgdisk=disabled"))

    def test_xinstall_compose_kernel_command_line_inc_purpose_opts4(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "xinstall" node.
        params = self.make_kernel_parameters(
            purpose="xinstall", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip6=off",
                    "ip=::::%s:BOOTIF" % params.hostname,
                ]
            ),
        )

    def test_xinstall_compose_kernel_command_line_inc_purpose_opts6(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "xinstall" node.
        params = self.make_kernel_parameters(
            purpose="xinstall", fs_host=factory.make_ipv6_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip=off",
                    "ip6=dhcp",
                ]
            ),
        )

    def test_xinstall_compose_kernel_command_line_inc_cc_datasource(self):
        # The result of compose_kernel_command_line includes the cloud-init
        # options for the datasource and cloud-config-url
        params = self.make_kernel_parameters(
            purpose="xinstall", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "cc:{'datasource_list': ['MAAS']}end_cc",
                    "cloud-config-url=%s" % params.preseed_url,
                ]
            ),
        )

    def test_ephemeral_compose_kernel_command_line_inc_cc_datasource(self):
        # The result of compose_kernel_command_line includes the cloud-init
        # options for the datasource and cloud-config-url
        params = self.make_kernel_parameters(
            purpose="ephemeral", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "cc:{'datasource_list': ['MAAS']}end_cc",
                    "cloud-config-url=%s" % params.preseed_url,
                ]
            ),
        )

    def test_commissioning_compose_kernel_command_line_inc_purpose_opts4(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "commissioning" node.
        params = self.make_kernel_parameters(
            purpose="commissioning", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip6=off",
                    "ip=::::%s:BOOTIF" % params.hostname,
                ]
            ),
        )

    def test_commissioning_compose_kernel_command_line_inc_purpose_opts6(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "commissioning" node.
        params = self.make_kernel_parameters(
            purpose="commissioning", fs_host=factory.make_ipv6_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip=off",
                    "ip6=dhcp",
                ]
            ),
        )

    def test_commissioning_compose_kernel_command_line_inc_cc_datasource(self):
        # The result of compose_kernel_command_line includes the cloud-init
        # options for the datasource and cloud-config-url
        params = self.make_kernel_parameters(
            purpose="commissioning", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "cc:{'datasource_list': ['MAAS']}end_cc",
                    "cloud-config-url=%s" % params.preseed_url,
                ]
            ),
        )

    def test_enlist_compose_kernel_command_line_inc_purpose_opts4(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "commissioning" node.
        params = self.make_kernel_parameters(
            purpose="enlist", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip6=off",
                    "ip=::::%s:BOOTIF" % params.hostname,
                ]
            ),
        )

    def test_enlist_compose_kernel_command_line_inc_purpose_opts6(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a non "commissioning" node.
        params = self.make_kernel_parameters(
            purpose="enlist", fs_host=factory.make_ipv6_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "root=squash:http://",
                    "overlayroot=tmpfs",
                    "ip=off",
                    "ip6=dhcp",
                ]
            ),
        )

    def test_enlist_compose_kernel_command_line_inc_cc_datasource(self):
        # The result of compose_kernel_command_line includes the cloud-init
        # options for the datasource and cloud-config-url
        params = self.make_kernel_parameters(
            purpose="enlist", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(
            cmdline,
            ContainsAll(
                [
                    "cc:{'datasource_list': ['MAAS']}end_cc",
                    "cloud-config-url=%s" % params.preseed_url,
                ]
            ),
        )

    def test_enlist_compose_kernel_command_line_apparmor_disabled(self):
        # The result of compose_kernel_command_line includes the
        # options for apparmor. See LP: #1677336 and LP: #1408106
        params = self.make_kernel_parameters(
            purpose="enlist", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, ContainsAll(["apparmor=0"]))

    def test_commissioning_compose_kernel_command_line_apparmor_disabled(self):
        # The result of compose_kernel_command_line includes the
        # options for apparmor. See LP: #1677336 and LP: #1408106
        params = self.make_kernel_parameters(
            purpose="commissioning", fs_host=factory.make_ipv4_address()
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, ContainsAll(["apparmor=0"]))

    def test_commissioning_compose_kernel_command_line_inc_extra_opts(self):
        mock_get_curtin_sep = self.patch(
            kernel_opts, "get_curtin_kernel_cmdline_sep"
        )
        sep = factory.make_name("sep")
        mock_get_curtin_sep.return_value = sep
        extra_opts = "special console=ABCD -- options to pass"
        params = self.make_kernel_parameters(extra_opts=extra_opts)
        cmdline = compose_kernel_command_line(params)
        # There should be KERNEL_CMDLINE_COPY_TO_INSTALL_SEP surrounded by
        # spaces before the options, but otherwise added verbatim.
        self.assertThat(cmdline, Contains(" %s " % sep + extra_opts))

    def test_commissioning_compose_kernel_handles_extra_opts_None(self):
        params = self.make_kernel_parameters(extra_opts=None)
        cmdline = compose_kernel_command_line(params)
        self.assertNotIn(cmdline, "None")

    def test_compose_kernel_command_line_inc_common_opts(self):
        # Test that some kernel arguments appear on commissioning, install
        # and xinstall command lines.
        expected = ["nomodeset"]

        params = self.make_kernel_parameters(
            purpose="commissioning", arch="i386"
        )
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, ContainsAll(expected))

        params = self.make_kernel_parameters(purpose="xinstall", arch="i386")
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, ContainsAll(expected))

        params = self.make_kernel_parameters(purpose="install", arch="i386")
        cmdline = compose_kernel_command_line(params)
        self.assertThat(cmdline, ContainsAll(expected))

    def test_compose_kernel_command_line_inc_purpose_opts_xinstall_node(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a "xinstall" node.
        params = self.make_kernel_parameters(purpose="xinstall")
        self.assertThat(
            compose_kernel_command_line(params),
            ContainsAll(["root=squash:http://"]),
        )

    def test_compose_kernel_command_line_inc_purpose_opts_comm_node(self):
        # The result of compose_kernel_command_line includes the purpose
        # options for a "commissioning" node.
        params = self.make_kernel_parameters(purpose="commissioning")
        self.assertThat(
            compose_kernel_command_line(params),
            ContainsAll(["root=squash:http://"]),
        )

    def test_compose_kernel_command_line_inc_arm_specific_option(self):
        params = self.make_kernel_parameters(arch="armhf", subarch="highbank")
        self.assertThat(
            compose_kernel_command_line(params), Contains("console=ttyAMA0")
        )

    def test_compose_kernel_command_line_not_inc_arm_specific_option(self):
        params = self.make_kernel_parameters(arch="i386")
        self.assertThat(
            compose_kernel_command_line(params),
            Not(Contains("console=ttyAMA0")),
        )

    def test_compose_arch_opts_copes_with_unknown_subarch(self):
        # Pass a None testcase so that the architecture doesn't get
        # registered.
        params = make_kernel_parameters(
            testcase=None,
            arch=factory.make_name("arch"),
            subarch=factory.make_name("subarch"),
        )
        self.assertEqual([], compose_arch_opts(params))

    def test_compose_rootfs_over_http_ipv4(self):
        params = make_kernel_parameters(fs_host=factory.make_ipv4_address())
        self.assertThat(
            compose_kernel_command_line(params),
            ContainsAll(
                [
                    "ro",
                    "root=squash:http://%s:5248/images/%s/%s/%s/%s/%s/squashfs"
                    % (
                        params.fs_host,
                        params.osystem,
                        params.arch,
                        params.subarch,
                        params.release,
                        params.label,
                    ),
                ]
            ),
        )

    def test_compose_rootfs_over_http_ipv6(self):
        params = make_kernel_parameters(fs_host=factory.make_ipv6_address())
        self.assertThat(
            compose_kernel_command_line(params),
            ContainsAll(
                [
                    "ro",
                    "root=squash:http://[%s]:5248/images/%s/%s/%s/%s/%s/squashfs"
                    % (
                        params.fs_host,
                        params.osystem,
                        params.arch,
                        params.subarch,
                        params.release,
                        params.label,
                    ),
                ]
            ),
        )
