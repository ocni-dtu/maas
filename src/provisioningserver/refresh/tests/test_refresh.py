# Copyright 2016-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Test refresh functions."""

__all__ = []

from collections import OrderedDict
from datetime import timedelta
import os
from pathlib import Path
import random
import sys
import tempfile
from textwrap import dedent
from unittest.mock import sentinel

from maastesting.factory import factory
from maastesting.matchers import Equals, MockAnyCall
from maastesting.testcase import MAASTestCase
from provisioningserver import refresh
from provisioningserver.refresh.maas_api_helper import (
    MD_VERSION,
    SignalException,
)
from provisioningserver.refresh.node_info_scripts import LXD_OUTPUT_NAME
from provisioningserver.utils.version import get_maas_version
from testtools.matchers import Contains, DirExists, Not


class TestHelpers(MAASTestCase):
    def test_get_architecture_returns_arch_with_generic(self):
        arch = random.choice(["i386", "amd64", "arm64", "ppc64el"])
        subarch = factory.make_name("subarch")
        self.patch_autospec(refresh, "call_and_check").return_value = (
            "%s/%s" % (arch, subarch)
        ).encode("utf-8")
        ret_arch = refresh.get_architecture()
        self.assertEquals("%s/generic" % arch, ret_arch)

    def test_get_architecture_returns_arch_with_subarch(self):
        arch = factory.make_name("arch")
        subarch = factory.make_name("subarch")
        architecture = "%s/%s" % (arch, subarch)
        self.patch_autospec(
            refresh, "call_and_check"
        ).return_value = architecture.encode("utf-8")
        ret_arch = refresh.get_architecture()
        self.assertEquals(architecture, ret_arch)

    def test_get_os_release_etc_os_release_exists(self):
        # This is a canary incase /etc/os-release ever goes away
        self.assertTrue(os.path.exists("/etc/os-release"))
        os_release = refresh.get_os_release()
        # refresh() in src/provisioningserver/rpc/clusterservice.py tries 'ID'
        # first and falls back on 'NAME' if its not found. Both exist in
        # Ubuntu 16.04 (Xenial).
        self.assertIn("ID", os_release)
        self.assertIn("NAME", os_release)
        # refresh() in src/provisioningserver/rpc/clusterservice.py tries
        # 'UBUNTU_CODENAME' first and falls back on 'VERSION_ID' if its not
        # found. Both exist in Ubuntu 16.04 (Xenial).
        self.assertIn("UBUNTU_CODENAME", os_release)
        self.assertIn("VERSION_ID", os_release)

    def test_get_sys_info(self):
        hostname = factory.make_hostname()
        osystem = factory.make_name("id")
        distro_series = factory.make_name("ubuntu_codename")
        architecture = factory.make_name("architecture")
        self.patch(refresh.socket, "gethostname").return_value = hostname
        self.patch_autospec(refresh, "get_os_release").return_value = {
            "ID": osystem,
            "UBUNTU_CODENAME": distro_series,
        }
        self.patch_autospec(
            refresh, "get_architecture"
        ).return_value = architecture
        self.assertThat(
            {
                "hostname": hostname,
                "architecture": architecture,
                "osystem": osystem,
                "distro_series": distro_series,
                "maas_version": get_maas_version(),
                "interfaces": {},
            },
            Equals(refresh.get_sys_info()),
        )

    def test_get_sys_info_on_host(self):
        self.assertNotIn(None, refresh.get_sys_info())

    def test_get_sys_info_alt(self):
        hostname = factory.make_hostname()
        osystem = factory.make_name("name")
        distro_series = factory.make_name("version_id")
        architecture = factory.make_name("architecture")
        self.patch(refresh.socket, "gethostname").return_value = hostname
        self.patch_autospec(refresh, "get_os_release").return_value = {
            "NAME": osystem,
            "VERSION_ID": distro_series,
        }
        self.patch_autospec(
            refresh, "get_architecture"
        ).return_value = architecture
        self.assertThat(
            {
                "hostname": hostname,
                "architecture": architecture,
                "osystem": osystem,
                "distro_series": distro_series,
                "maas_version": get_maas_version(),
                "interfaces": {},
            },
            Equals(refresh.get_sys_info()),
        )

    def test_get_sys_info_empty(self):
        hostname = factory.make_hostname()
        architecture = factory.make_name("architecture")
        self.patch(refresh.socket, "gethostname").return_value = hostname
        self.patch_autospec(refresh, "get_os_release").return_value = {}
        self.patch_autospec(
            refresh, "get_architecture"
        ).return_value = architecture
        self.assertThat(
            {
                "hostname": hostname,
                "architecture": architecture,
                "osystem": "",
                "distro_series": "",
                "maas_version": get_maas_version(),
                "interfaces": {},
            },
            Equals(refresh.get_sys_info()),
        )


class TestRefresh(MAASTestCase):
    def setUp(self):
        super().setUp()
        # When running scripts in a tty MAAS outputs the results to help with
        # debug. Quiet the output when running in tests.
        self.patch(sys, "stdout")

    def patch_scripts_success(
        self, script_name=None, timeout=None, script_content=None
    ):
        if script_name is None:
            script_name = factory.make_name("script_name")
        if timeout is None:
            timeout = timedelta(seconds=random.randint(1, 500))
        if script_content is None:
            script_content = dedent(
                """\
                #!/bin/bash
                echo 'test script'
                """
            )
        refresh.NODE_INFO_SCRIPTS = OrderedDict(
            [
                (
                    script_name,
                    {
                        "content": script_content.encode("ascii"),
                        "name": script_name,
                        "run_on_controller": True,
                        "timeout": timeout,
                    },
                )
            ]
        )

    def patch_scripts_failure(self, script_name=None, timeout=None):
        if script_name is None:
            script_name = factory.make_name("script_name")
        if timeout is None:
            timeout = timedelta(seconds=random.randint(1, 500))
        TEST_SCRIPT = dedent(
            """\
            #!/bin/bash
            echo 'test failed'
            exit 1
            """
        )
        refresh.NODE_INFO_SCRIPTS = OrderedDict(
            [
                (
                    script_name,
                    {
                        "content": TEST_SCRIPT.encode("ascii"),
                        "name": script_name,
                        "run_on_controller": True,
                        "timeout": timeout,
                    },
                )
            ]
        )

    def test_refresh_accepts_defined_url(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_success(script_name)

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                "Starting %s [1/1]" % script_name,
            ),
        )

    def test_refresh_signals_starting(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_success(script_name)

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                "Starting %s [1/1]" % script_name,
            ),
        )

    def test_refresh_signals_results(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        script_content = dedent(
            """\
        #!/bin/bash
        echo 'test script'
        echo '{status: skipped}' > $RESULT_PATH
        """
        )
        self.patch_scripts_success(script_name, script_content=script_content)

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                files={
                    script_name: b"test script\n",
                    "%s.out" % script_name: b"test script\n",
                    "%s.err" % script_name: b"",
                    "%s.yaml" % script_name: b"{status: skipped}\n",
                },
                exit_status=0,
                error="Finished %s [1/1]: 0" % script_name,
            ),
        )

    def test_refresh_sets_env_vars(self):
        self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_failure(script_name)
        mock_popen = self.patch(refresh, "Popen")
        mock_popen.side_effect = OSError(8, "Exec format error")

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)

        env = mock_popen.call_args[1]["env"]
        for name in [
            "OUTPUT_COMBINED_PATH",
            "OUTPUT_STDOUT_PATH",
            "OUTPUT_STDERR_PATH",
            "RESULT_PATH",
        ]:
            self.assertIn(name, env)
            self.assertIn(script_name, env[name])

    def test_refresh_signals_failure_on_unexecutable_script(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_failure(script_name)
        mock_popen = self.patch(refresh, "Popen")
        mock_popen.side_effect = OSError(8, "Exec format error")

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                files={
                    script_name: b"[Errno 8] Exec format error",
                    "%s.err" % script_name: b"[Errno 8] Exec format error",
                },
                exit_status=8,
                error="Failed to execute %s [1/1]: 8" % script_name,
            ),
        )

    def test_refresh_signals_failure_on_unexecutable_script_no_errno(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_failure(script_name)
        mock_popen = self.patch(refresh, "Popen")
        mock_popen.side_effect = OSError()

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                files={
                    script_name: b"Unable to execute script",
                    "%s.err" % script_name: b"Unable to execute script",
                },
                exit_status=2,
                error="Failed to execute %s [1/1]: 2" % script_name,
            ),
        )

    def test_refresh_signals_failure_on_unexecutable_script_baderrno(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_failure(script_name)
        mock_popen = self.patch(refresh, "Popen")
        mock_popen.side_effect = OSError(0, "Exec format error")

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                files={
                    script_name: b"[Errno 0] Exec format error",
                    "%s.err" % script_name: b"[Errno 0] Exec format error",
                },
                exit_status=2,
                error="Failed to execute %s [1/1]: 2" % script_name,
            ),
        )

    def test_refresh_signals_failure_on_timeout(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        timeout = timedelta(seconds=random.randint(1, 500))
        self.patch_scripts_failure(script_name, timeout)
        self.patch(refresh.maas_api_helper.time, "monotonic").side_effect = (
            0,
            timeout.seconds + (60 * 6),
            timeout.seconds + (60 * 6),
        )

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "TIMEDOUT",
                files={
                    script_name: b"test failed\n",
                    "%s.out" % script_name: b"test failed\n",
                    "%s.err" % script_name: b"",
                },
                error="Timeout(%s) expired on %s [1/1]"
                % (str(timeout), script_name),
            ),
        )

    def test_refresh_signals_finished(self):
        signal = self.patch(refresh, "signal")
        script_name = factory.make_name("script_name")
        self.patch_scripts_success(script_name)

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "OK",
                "Finished refreshing %s" % system_id,
            ),
        )

    def test_refresh_signals_failure(self):
        signal = self.patch(refresh, "signal")
        self.patch_scripts_failure()

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "FAILED",
                "Failed refreshing %s" % system_id,
            ),
        )

    def test_refresh_executes_lxd_binary(self):
        signal = self.patch(refresh, "signal")
        script_name = LXD_OUTPUT_NAME
        self.patch_scripts_success(script_name)

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                "Starting %s [1/1]" % script_name,
            ),
        )

    def test_refresh_executes_lxd_binary_in_snap(self):
        signal = self.patch(refresh, "signal")
        script_name = LXD_OUTPUT_NAME
        self.patch_scripts_success(script_name)
        path = factory.make_name()
        self.patch(os, "environ", {"SNAP": path})

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)
        self.assertThat(
            signal,
            MockAnyCall(
                "%s/metadata/%s/" % (url, MD_VERSION),
                {
                    "consumer_secret": "",
                    "consumer_key": consumer_key,
                    "token_key": token_key,
                    "token_secret": token_secret,
                },
                "WORKING",
                "Starting %s [1/1]" % script_name,
            ),
        )

    def test_refresh_clears_up_temporary_directory(self):

        ScriptsBroken = factory.make_exception_type()

        def find_temporary_directories():
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir).absolute()
                return {
                    str(entry)
                    for entry in tmpdir.parent.iterdir()
                    if entry.is_dir() and entry != tmpdir
                }

        tmpdirs_during = set()
        tmpdir_during = None

        def runscripts(*args, tmpdir):
            self.assertThat(tmpdir, DirExists())
            nonlocal tmpdirs_during, tmpdir_during
            tmpdirs_during |= find_temporary_directories()
            tmpdir_during = tmpdir
            raise ScriptsBroken("Foom")

        self.patch(refresh, "runscripts", runscripts)

        tmpdirs_before = find_temporary_directories()
        self.assertRaises(
            ScriptsBroken,
            refresh.refresh,
            sentinel.system_id,
            sentinel.consumer_key,
            sentinel.token_key,
            sentinel.token_secret,
        )
        tmpdirs_after = find_temporary_directories()

        self.assertThat(tmpdirs_before, Not(Contains(tmpdir_during)))
        self.assertThat(tmpdirs_during, Contains(tmpdir_during))
        self.assertThat(tmpdirs_after, Not(Contains(tmpdir_during)))

    def test_refresh_logs_error(self):
        signal = self.patch(refresh, "signal")
        maaslog = self.patch(refresh.maaslog, "error")
        error = factory.make_string()
        signal.side_effect = SignalException(error)
        self.patch_scripts_failure()

        system_id = factory.make_name("system_id")
        consumer_key = factory.make_name("consumer_key")
        token_key = factory.make_name("token_key")
        token_secret = factory.make_name("token_secret")
        url = factory.make_url()

        refresh.refresh(system_id, consumer_key, token_key, token_secret, url)

        self.assertThat(
            maaslog, MockAnyCall("Error during controller refresh: %s" % error)
        )
