# Copyright 2017-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for maas_run_remote_scripts.py."""

__all__ = []

import copy
from datetime import timedelta
import http.client
from io import BytesIO
import json
import os
import random
import stat
from subprocess import CalledProcessError, DEVNULL, PIPE, TimeoutExpired
import tarfile
import time
from unittest.mock import ANY, call, MagicMock
from zipfile import ZipFile

from maastesting.factory import factory
from maastesting.fixtures import TempDirectory
from maastesting.matchers import (
    MockAnyCall,
    MockCalledOnce,
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from snippets import maas_run_remote_scripts
from snippets.maas_api_helper import SignalException
from snippets.maas_run_remote_scripts import (
    _check_link_connected,
    CustomNetworking,
    download_and_extract_tar,
    get_block_devices,
    get_interfaces,
    install_dependencies,
    output_and_send,
    output_and_send_scripts,
    parse_parameters,
    run_and_check,
    run_script,
    run_scripts,
    run_scripts_from_metadata,
)
import yaml

# Unused ScriptResult id, used to make sure number is always unique.
SCRIPT_RESULT_ID = 0


def make_script(
    scripts_dir=None,
    with_added_attribs=True,
    name=None,
    script_version_id=None,
    timeout_seconds=None,
    parallel=None,
    hardware_type=None,
    apply_configured_networking=False,
    with_output=True,
):
    if name is None:
        name = factory.make_name("name")
    if script_version_id is None:
        script_version_id = random.randint(1, 1000)
    if timeout_seconds is None:
        timeout_seconds = random.randint(1, 1000)
    if parallel is None:
        parallel = random.randint(0, 2)
    if hardware_type is None:
        hardware_type = random.randint(0, 4)
    global SCRIPT_RESULT_ID
    script_result_id = SCRIPT_RESULT_ID
    SCRIPT_RESULT_ID += 1
    ret = {
        "name": name,
        "path": "%s/%s" % (random.choice(["commissioning", "testing"]), name),
        "script_result_id": script_result_id,
        "script_version_id": script_version_id,
        "timeout_seconds": timeout_seconds,
        "parallel": parallel,
        "hardware_type": hardware_type,
        "has_started": factory.pick_bool(),
        "apply_configured_networking": apply_configured_networking,
    }
    ret["msg_name"] = "%s (id: %s, script_version_id: %s)" % (
        name,
        script_result_id,
        script_version_id,
    )
    if with_added_attribs:
        if scripts_dir is None:
            scripts_dir = factory.make_name("scripts_dir")
        out_dir = os.path.join(
            scripts_dir, "out", "%s.%s" % (name, script_result_id)
        )

        ret["args"] = {
            "url": factory.make_url(),
            "creds": factory.make_name("creds"),
            "script_result_id": script_result_id,
        }
        ret["msg_name"] = "%s (id: %s, script_version_id: %s)" % (
            name,
            script_result_id,
            script_version_id,
        )
        ret["combined_name"] = name
        ret["combined_path"] = os.path.join(out_dir, ret["combined_name"])
        ret["combined"] = factory.make_string()
        ret["stdout_name"] = "%s.out" % name
        ret["stdout_path"] = os.path.join(out_dir, ret["stdout_name"])
        ret["stdout"] = factory.make_string()
        ret["stderr_name"] = "%s.err" % name
        ret["stderr_path"] = os.path.join(out_dir, ret["stderr_name"])
        ret["stderr"] = factory.make_string()
        ret["result_name"] = "%s.yaml" % name
        ret["result_path"] = os.path.join(out_dir, ret["result_name"])
        ret["result"] = yaml.safe_dump(
            {factory.make_string(): factory.make_string()}
        )
        ret["download_path"] = os.path.join(scripts_dir, "downloads", name)

        if os.path.exists(scripts_dir):
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(ret["download_path"], exist_ok=True)
            script_path = os.path.join(scripts_dir, ret["path"])
            os.makedirs(os.path.dirname(script_path), exist_ok=True)
            with open(os.path.join(scripts_dir, ret["path"]), "w") as f:
                f.write("#!/bin/bash")
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)

            if with_output:
                open(ret["combined_path"], "w").write(ret["combined"])
                open(ret["stdout_path"], "w").write(ret["stdout"])
                open(ret["stderr_path"], "w").write(ret["stderr"])
                open(ret["result_path"], "w").write(ret["result"])

    return ret


def make_scripts(
    instance=True,
    count=3,
    scripts_dir=None,
    with_added_attribs=True,
    with_output=True,
    parallel=None,
    hardware_type=None,
    apply_configured_networking=None,
):
    if instance:
        script = make_script(
            scripts_dir=scripts_dir,
            with_added_attribs=with_added_attribs,
            with_output=with_output,
            parallel=parallel,
            hardware_type=hardware_type,
            apply_configured_networking=apply_configured_networking,
        )
        return [script] + [
            make_script(
                scripts_dir=scripts_dir,
                with_added_attribs=with_added_attribs,
                with_output=with_output,
                name=script["name"],
                script_version_id=script["script_version_id"],
                timeout_seconds=script["timeout_seconds"],
                parallel=script["parallel"],
                hardware_type=script["hardware_type"],
                apply_configured_networking=script[
                    "apply_configured_networking"
                ],
            )
            for _ in range(count - 1)
        ]
    else:
        return [
            make_script(
                scripts_dir=scripts_dir,
                with_added_attribs=with_added_attribs,
                with_output=with_output,
                parallel=parallel,
            )
            for _ in range(count)
        ]


def make_fake_os_path_exists(testcase, exists=True):
    orig_os_path_exists = os.path.exists

    def fake_os_path_exists(path):
        if path == "/var/cache/apt/pkgcache.bin":
            return exists
        return orig_os_path_exists(path)

    testcase.patch(
        maas_run_remote_scripts.os.path, "exists"
    ).side_effect = fake_os_path_exists


class TestOutputAndSend(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.stdout_write = self.patch(
            maas_run_remote_scripts.sys.stdout, "write"
        )
        self.stdout_flush = self.patch(
            maas_run_remote_scripts.sys.stdout, "flush"
        )
        self.signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )

    def test_output_and_send_outputs(self):
        error = factory.make_string()
        output_and_send(error)

        self.assertThat(self.stdout_write, MockCalledOnceWith("%s\n" % error))
        self.assertThat(self.stdout_flush, MockCalledOnce())
        self.assertThat(self.signal_wrapper, MockCalledOnceWith(error=error))

    def test_output_and_send_doesnt_send_when_false(self):
        error = factory.make_string()
        output_and_send(error, False)

        self.assertThat(self.stdout_write, MockCalledOnceWith("%s\n" % error))
        self.assertThat(self.stdout_flush, MockCalledOnce())
        self.assertThat(self.signal_wrapper, MockNotCalled())

    def test_output_and_send_scripts(self):
        scripts = make_scripts()
        error = "{msg_name} %s" % factory.make_string()
        output_and_send_scripts(error, scripts)

        self.assertThat(
            self.stdout_write,
            MockCallsMatch(
                *[call("%s\n" % error.format(**script)) for script in scripts]
            ),
        )

    def test_output_and_send_scripts_sets_error_as_stderr(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        error = "{msg_name} %s" % factory.make_string()
        output_and_send_scripts(error, scripts, error_is_stderr=True)

        self.assertThat(
            self.signal_wrapper,
            MockCallsMatch(
                *[
                    call(
                        error=error.format(**script),
                        **script["args"],
                        files={
                            script["combined_name"]: (
                                b"%s\n" % error.format(**script).encode()
                            ),
                            script["stderr_name"]: (
                                b"%s\n" % error.format(**script).encode()
                            ),
                        }
                    )
                    for script in scripts
                ]
            ),
        )
        for script in scripts:
            script_error = "%s\n" % error.format(**script)
            self.assertEquals(
                script_error, open(script["combined_path"], "r").read()
            )
            self.assertEquals(
                script_error, open(script["stderr_path"], "r").read()
            )


class TestInstallDependencies(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.mock_output_and_send = self.patch(
            maas_run_remote_scripts, "output_and_send"
        )
        self.mock_output_and_send_scripts = self.patch(
            maas_run_remote_scripts, "output_and_send_scripts"
        )
        self.patch(maas_run_remote_scripts.sys.stdout, "write")
        self.patch(maas_run_remote_scripts.sys.stderr, "write")

    def test_run_and_check(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        script = scripts[0]

        self.assertTrue(
            run_and_check(
                [
                    "/bin/bash",
                    "-c",
                    "echo %s;echo %s >&2"
                    % (script["stdout"], script["stderr"]),
                ],
                scripts,
                factory.make_name("status"),
            )
        )
        self.assertEquals(
            "%s\n" % script["stdout"], open(script["stdout_path"], "r").read()
        )
        self.assertEquals(
            "%s\n" % script["stderr"], open(script["stderr_path"], "r").read()
        )
        self.assertEquals(
            "%s\n%s\n" % (script["stdout"], script["stderr"]),
            open(script["combined_path"], "r").read(),
        )

    def test_run_and_check_errors(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        script = scripts[0]
        status = factory.make_name("status")

        self.assertFalse(
            run_and_check(
                [
                    "/bin/bash",
                    "-c",
                    "echo %s;echo %s >&2;false"
                    % (script["stdout"], script["stderr"]),
                ],
                scripts,
                status,
            )
        )
        self.assertEquals(
            "%s\n" % script["stdout"], open(script["stdout_path"], "r").read()
        )
        self.assertEquals(
            "%s\n" % script["stderr"], open(script["stderr_path"], "r").read()
        )
        self.assertEquals(
            "%s\n%s\n" % (script["stdout"], script["stderr"]),
            open(script["combined_path"], "r").read(),
        )
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send,
                MockAnyCall(
                    "Failed installing package(s) for %s" % script["msg_name"],
                    exit_status=1,
                    status=status,
                    **script["args"],
                    files={
                        scripts[0]["combined_name"]: (
                            "%s\n%s\n"
                            % (scripts[0]["stdout"], scripts[0]["stderr"])
                        ).encode(),
                        scripts[0]["stdout_name"]: (
                            "%s\n" % scripts[0]["stdout"]
                        ).encode(),
                        scripts[0]["stderr_name"]: (
                            "%s\n" % scripts[0]["stderr"]
                        ).encode(),
                    }
                ),
            )

    def test_run_and_check_ignores_errors(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        script = scripts[0]

        self.assertTrue(
            run_and_check(
                [
                    "/bin/bash",
                    "-c",
                    "echo %s;echo %s >&2;false"
                    % (script["stdout"], script["stderr"]),
                ],
                scripts,
                factory.make_name("status"),
                False,
            )
        )
        self.assertEquals(
            "%s\n" % script["stdout"], open(script["stdout_path"], "r").read()
        )
        self.assertEquals(
            "%s\n" % script["stderr"], open(script["stderr_path"], "r").read()
        )
        self.assertEquals(
            "%s\n%s\n" % (script["stdout"], script["stderr"]),
            open(script["combined_path"], "r").read(),
        )

    def test_sudo_run_and_check(self):
        mock_popen = self.patch(maas_run_remote_scripts, "Popen")
        self.patch(maas_run_remote_scripts, "capture_script_output")
        cmd = factory.make_name("cmd")

        run_and_check(
            [cmd], MagicMock(), factory.make_name("status"), False, True
        )

        self.assertThat(
            mock_popen,
            MockCalledOnceWith(
                ["sudo", "-En", cmd], stdin=DEVNULL, stdout=PIPE, stderr=PIPE
            ),
        )

    def test_run_and_check_calls_hook_on_failure(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        script = scripts[0]
        status = factory.make_name("status")
        mock_failure_hook = MagicMock()

        self.assertFalse(
            run_and_check(
                [
                    "/bin/bash",
                    "-c",
                    "echo %s;echo %s >&2;false"
                    % (script["stdout"], script["stderr"]),
                ],
                scripts,
                status,
                failure_hook=mock_failure_hook,
            )
        )
        self.assertThat(mock_failure_hook, MockCalledOnceWith())

    def test_install_dependencies_does_nothing_when_empty(self):
        self.assertTrue(install_dependencies([]))
        self.assertThat(self.mock_output_and_send, MockNotCalled())

    def test_install_dependencies_does_nothing_when_no_packages(self):
        self.assertTrue(install_dependencies(make_scripts()))
        self.assertThat(self.mock_output_and_send, MockNotCalled())

    def test_install_dependencies_apt(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        make_fake_os_path_exists(self)
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [factory.make_name("apt_pkg") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"apt": packages}

        self.assertTrue(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing apt packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
            self.assertThat(
                mock_run_and_check,
                MockCalledOnceWith(
                    ["apt-get", "-qy", "--no-install-recommends", "install"]
                    + packages,
                    scripts,
                    "INSTALLING",
                    True,
                    True,
                ),
            )
            # Verify cleanup
            self.assertFalse(os.path.exists(script["combined_path"]))
            self.assertFalse(os.path.exists(script["stdout_path"]))
            self.assertFalse(os.path.exists(script["stderr_path"]))

    def test_install_dependencies_runs_apt_get_update_when_required(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        make_fake_os_path_exists(self, False)
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [factory.make_name("apt_pkg") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"apt": packages}

        self.assertTrue(install_dependencies(scripts))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["apt-get", "-qy", "update"], scripts, "INSTALLING", True, True
            ),
        )
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing apt packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
            self.assertThat(
                mock_run_and_check,
                MockAnyCall(
                    ["apt-get", "-qy", "--no-install-recommends", "install"]
                    + packages,
                    scripts,
                    "INSTALLING",
                    True,
                    True,
                ),
            )
            # Verify cleanup
            self.assertFalse(os.path.exists(script["combined_path"]))
            self.assertFalse(os.path.exists(script["stdout_path"]))
            self.assertFalse(os.path.exists(script["stderr_path"]))

    def test_install_dependencies_apt_errors(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        mock_run_and_check.return_value = False
        make_fake_os_path_exists(self)
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [factory.make_name("apt_pkg") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"apt": packages}

        self.assertFalse(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing apt packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
            self.assertThat(
                mock_run_and_check,
                MockCalledOnceWith(
                    ["apt-get", "-qy", "--no-install-recommends", "install"]
                    + packages,
                    scripts,
                    "INSTALLING",
                    True,
                    True,
                ),
            )

    def test_install_dependencies_snap_str_list(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [factory.make_name("snap_pkg") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"snap": packages}

        self.assertTrue(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing snap packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
            # Verify cleanup
            self.assertFalse(os.path.exists(script["combined_path"]))
            self.assertFalse(os.path.exists(script["stdout_path"]))
            self.assertFalse(os.path.exists(script["stderr_path"]))

        for package in packages:
            self.assertThat(
                mock_run_and_check,
                MockAnyCall(
                    ["snap", "install", package],
                    scripts,
                    "INSTALLING",
                    True,
                    True,
                ),
            )

    def test_install_dependencies_snap_str_dict(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [
            {"name": factory.make_name("pkg")},
            {
                "name": factory.make_name("pkg"),
                "channel": random.choice(
                    ["edge", "beta", "candidate", "stable"]
                ),
            },
            {
                "name": factory.make_name("pkg"),
                "channel": random.choice(
                    ["edge", "beta", "candidate", "stable"]
                ),
                "mode": random.choice(["dev", "jail"]),
            },
            {
                "name": factory.make_name("pkg"),
                "channel": random.choice(
                    ["edge", "beta", "candidate", "stable"]
                ),
                "mode": random.choice(["dev", "jail"]),
            },
        ]
        for script in scripts:
            script["packages"] = {"snap": packages}

        self.assertTrue(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing snap packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
            # Verify cleanup
            self.assertFalse(os.path.exists(script["combined_path"]))
            self.assertFalse(os.path.exists(script["stdout_path"]))
            self.assertFalse(os.path.exists(script["stderr_path"]))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["snap", "install", packages[0]["name"]],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                [
                    "snap",
                    "install",
                    packages[1]["name"],
                    "--%s" % packages[1]["channel"],
                ],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                [
                    "snap",
                    "install",
                    packages[2]["name"],
                    "--%s" % packages[2]["channel"],
                    "--%smode" % packages[2]["mode"],
                ],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                [
                    "snap",
                    "install",
                    packages[3]["name"],
                    "--%s" % packages[3]["channel"],
                    "--%smode" % packages[3]["mode"],
                ],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )

    def test_install_dependencies_snap_errors(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        mock_run_and_check.return_value = False
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        packages = [factory.make_name("snap_pkg") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"snap": packages}

        self.assertFalse(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Installing snap packages for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )

        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["snap", "install", packages[0]],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )

    def test_install_dependencies_url(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir)
        packages = [factory.make_name("url") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"url": packages}

        self.assertTrue(install_dependencies(scripts))
        for package in packages:
            self.assertThat(
                mock_run_and_check,
                MockAnyCall(
                    ["wget", package, "-P", scripts[0]["download_path"]],
                    scripts,
                    "INSTALLING",
                    True,
                ),
            )
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Downloading and extracting URLs for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )
        # Verify cleanup
        self.assertFalse(os.path.exists(scripts[0]["combined_path"]))
        self.assertFalse(os.path.exists(scripts[0]["stdout_path"]))
        self.assertFalse(os.path.exists(scripts[0]["stderr_path"]))

    def test_install_dependencies_url_errors(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        mock_run_and_check.return_value = False
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir)
        packages = [factory.make_name("url") for _ in range(3)]
        for script in scripts:
            script["packages"] = {"url": packages}

        self.assertFalse(install_dependencies(scripts))
        for script in scripts:
            self.assertThat(
                self.mock_output_and_send_scripts,
                MockAnyCall(
                    "Downloading and extracting URLs for {msg_name}",
                    scripts,
                    True,
                    status="INSTALLING",
                ),
            )

    def test_install_dependencies_url_tar(self):
        self.patch(maas_run_remote_scripts, "run_and_check")
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        tar_file = os.path.join(scripts[0]["download_path"], "file.tar.xz")
        file_content = factory.make_bytes()
        with tarfile.open(tar_file, "w:xz") as tar:
            tarinfo = tarfile.TarInfo(name="test-file")
            tarinfo.size = len(file_content)
            tarinfo.mode = 0o755
            tar.addfile(tarinfo, BytesIO(file_content))
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % tar_file)
        for script in scripts:
            script["packages"] = {"url": [tar_file]}

        self.assertTrue(install_dependencies(scripts))
        with open(
            os.path.join(scripts[0]["download_path"], "test-file"), "rb"
        ) as f:
            self.assertEquals(file_content, f.read())

    def test_install_dependencies_url_zip(self):
        self.patch(maas_run_remote_scripts, "run_and_check")
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        zip_file = os.path.join(scripts[0]["download_path"], "file.zip")
        file_content = factory.make_bytes()
        with ZipFile(zip_file, "w") as z:
            z.writestr("test-file", file_content)
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % zip_file)
        for script in scripts:
            script["packages"] = {"url": [zip_file]}

        self.assertTrue(install_dependencies(scripts))
        with open(
            os.path.join(scripts[0]["download_path"], "test-file"), "rb"
        ) as f:
            self.assertEquals(file_content, f.read())

    def test_install_dependencies_url_deb(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        deb_file = os.path.join(scripts[0]["download_path"], "file.deb")
        open(deb_file, "w").close()
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % deb_file)
        for script in scripts:
            script["packages"] = {"url": [deb_file]}

        self.assertTrue(install_dependencies(scripts))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["dpkg", "-i", deb_file], scripts, "INSTALLING", False, True
            ),
        )
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["apt-get", "install", "-qyf", "--no-install-recommends"],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )

    def test_install_dependencies_url_deb_errors(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        mock_run_and_check.side_effect = (True, True, False)
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        deb_file = os.path.join(scripts[0]["download_path"], "file.deb")
        open(deb_file, "w").close()
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % deb_file)
        for script in scripts:
            script["packages"] = {"url": [deb_file]}

        self.assertFalse(install_dependencies(scripts))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["dpkg", "-i", deb_file], scripts, "INSTALLING", False, True
            ),
        )
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["apt-get", "install", "-qyf", "--no-install-recommends"],
                scripts,
                "INSTALLING",
                True,
                True,
            ),
        )

    def test_install_dependencies_url_snap(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        snap_file = os.path.join(scripts[0]["download_path"], "file.snap")
        open(snap_file, "w").close()
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % snap_file)
        for script in scripts:
            script["packages"] = {"url": [snap_file]}

        self.assertTrue(install_dependencies(scripts))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["snap", snap_file], scripts, "INSTALLING", True, True
            ),
        )

    def test_install_dependencies_url_snap_errors(self):
        mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        mock_run_and_check.side_effect = (True, False)
        scripts_dir = self.useFixture(TempDirectory()).path
        scripts = make_scripts(scripts_dir=scripts_dir, with_output=False)
        snap_file = os.path.join(scripts[0]["download_path"], "file.snap")
        open(snap_file, "w").close()
        with open(scripts[0]["combined_path"], "w") as output:
            output.write("Saving to: '%s'" % snap_file)
        for script in scripts:
            script["packages"] = {"url": [snap_file]}

        self.assertFalse(install_dependencies(scripts))
        self.assertThat(
            mock_run_and_check,
            MockAnyCall(
                ["snap", snap_file], scripts, "INSTALLING", True, True
            ),
        )


class TestCustomNetworking(MAASTestCase):
    def setUp(self):
        super().setUp()

        self.base_dir = self.useFixture(TempDirectory()).path
        self.config_dir = os.path.join(self.base_dir, "config")
        os.makedirs(self.config_dir, exist_ok=True)
        self.netplan_dir = os.path.join(self.base_dir, "netplan")
        maas_run_remote_scripts.NETPLAN_DIR = self.netplan_dir
        os.makedirs(self.netplan_dir, exist_ok=True)

        self.mock_output_and_send_scripts = self.patch(
            maas_run_remote_scripts, "output_and_send_scripts"
        )
        self.mock_run_and_check = self.patch(
            maas_run_remote_scripts, "run_and_check"
        )
        self.mock_get_interfaces = self.patch(
            maas_run_remote_scripts, "get_interfaces"
        )
        self.mock_signal = self.patch(maas_run_remote_scripts, "signal")
        self.patch(maas_run_remote_scripts.sys.stdout, "write")
        self.patch(maas_run_remote_scripts.sys.stderr, "write")
        self.patch(maas_run_remote_scripts.time, "sleep")

    def test_enter_does_nothing_if_not_required(self):
        custom_networking = CustomNetworking(
            make_scripts(apply_configured_networking=False), self.config_dir
        )
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )
        custom_networking.__enter__()

        self.assertThat(self.mock_output_and_send_scripts, MockNotCalled())
        self.assertThat(mock_bring_down_networking, MockNotCalled())

    def test_enter_raises_filenotfounderror_if_netplan_yaml_missing(self):
        scripts = make_scripts(apply_configured_networking=True)
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        self.assertRaises(FileNotFoundError, custom_networking.__enter__)
        self.assertThat(
            self.mock_output_and_send_scripts,
            MockCallsMatch(
                call(ANY, scripts, True, status="APPLYING_NETCONF"),
                call(
                    ANY,
                    scripts,
                    True,
                    error_is_stderr=True,
                    exit_status=1,
                    status="APPLYING_NETCONF",
                ),
            ),
        )
        self.assertThat(mock_bring_down_networking, MockNotCalled())

    def test_enter_applies_custom_networking(self):
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.config_dir, "netplan.yaml", netplan_yaml_content
        )
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        with custom_networking:
            # Verify config files where moved into place
            self.assertEquals(
                netplan_yaml_content,
                open(
                    os.path.join(self.netplan_dir, "netplan.yaml"), "r"
                ).read(),
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(self.netplan_dir, "50-cloud-init.yaml")
                )
            )
            self.assertEquals(
                ephemeral_config_content,
                open(
                    os.path.join(
                        self.config_dir, "netplan.bak", "50-cloud-init.yaml"
                    ),
                    "r",
                ).read(),
            )
            # Verify logs from netplan apply where removed
            for path in ["combined_path", "stdout_path", "stderr_path"]:
                self.assertFalse(os.path.exists(scripts[0][path]))
            # Disable applying ephemeral netplan
            custom_networking.apply_configured_networking = False

        self.assertThat(self.mock_output_and_send_scripts, MockCalledOnce())
        self.assertThat(self.mock_run_and_check, MockCalledOnce())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(self.mock_signal, MockCalledOnce())
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_enter_applies_custom_networking_no_send(self):
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.config_dir, "netplan.yaml", netplan_yaml_content
        )
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir, False)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        with custom_networking:
            # Verify config files where moved into place
            self.assertEquals(
                netplan_yaml_content,
                open(
                    os.path.join(self.netplan_dir, "netplan.yaml"), "r"
                ).read(),
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(self.netplan_dir, "50-cloud-init.yaml")
                )
            )
            self.assertEquals(
                ephemeral_config_content,
                open(
                    os.path.join(
                        self.config_dir, "netplan.bak", "50-cloud-init.yaml"
                    ),
                    "r",
                ).read(),
            )
            # Verify logs from netplan apply where removed
            for path in ["combined_path", "stdout_path", "stderr_path"]:
                self.assertFalse(os.path.exists(scripts[0][path]))
            # Disable applying ephemeral netplan
            custom_networking.apply_configured_networking = False

        self.assertThat(self.mock_output_and_send_scripts, MockCalledOnce())
        self.assertThat(self.mock_run_and_check, MockCalledOnce())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(self.mock_signal, MockNotCalled())
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_enter_raises_oserror_when_netplan_apply_fails(self):
        factory.make_file(self.config_dir, "netplan.yaml")
        factory.make_file(self.netplan_dir, "50-cloud-init.yaml")
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        self.mock_run_and_check.return_value = False
        mock_clean_logs = self.patch(maas_run_remote_scripts, "_clean_logs")

        self.assertRaises(OSError, custom_networking.__enter__)
        self.assertThat(self.mock_output_and_send_scripts, MockCalledOnce())
        self.assertThat(self.mock_get_interfaces, MockNotCalled())
        self.assertThat(self.mock_signal, MockNotCalled())
        self.assertThat(mock_clean_logs, MockNotCalled())
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_enter_raises_signalexception_when_signal_fails(self):
        factory.make_file(self.config_dir, "netplan.yaml")
        factory.make_file(self.netplan_dir, "50-cloud-init.yaml")
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_apply_ephemeral_netplan = self.patch(
            custom_networking, "_apply_ephemeral_netplan"
        )
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )
        self.mock_signal.side_effect = SignalException(
            factory.make_name("error")
        )
        mock_clean_logs = self.patch(maas_run_remote_scripts, "_clean_logs")

        self.assertRaises(SignalException, custom_networking.__enter__)
        self.assertThat(
            self.mock_output_and_send_scripts,
            MockCallsMatch(
                call(ANY, scripts, True, status="APPLYING_NETCONF"),
                call(
                    ANY,
                    scripts,
                    True,
                    error_is_stderr=True,
                    exit_status=1,
                    status="APPLYING_NETCONF",
                ),
            ),
        )
        self.assertThat(self.mock_run_and_check, MockCalledOnce())
        self.assertThat(self.mock_get_interfaces, MockCalledOnce())
        self.assertThat(self.mock_signal, MockCalledOnce())
        self.assertThat(mock_apply_ephemeral_netplan, MockCalledOnce())
        self.assertThat(mock_clean_logs, MockNotCalled())
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_apply_ephemeral_netplan_does_nothing_if_not_backup_config(self):
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, factory.make_name())
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        custom_networking._apply_ephemeral_netplan()

        self.assertThat(mock_check_call, MockNotCalled())
        self.assertThat(mock_signal_wrapper, MockNotCalled())
        self.assertThat(self.mock_get_interfaces, MockNotCalled())
        self.assertThat(mock_bring_down_networking, MockNotCalled())

    def test_apply_ephemeral_netplan(self):
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "netplan.yaml", netplan_yaml_content
        )
        backup_dir = os.path.join(self.config_dir, "netplan.bak")
        os.makedirs(backup_dir, exist_ok=True)
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            backup_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        custom_networking._apply_ephemeral_netplan()

        self.assertFalse(os.path.exists(backup_dir))
        self.assertEquals(
            ephemeral_config_content,
            open(
                os.path.join(self.netplan_dir, "50-cloud-init.yaml"), "r"
            ).read(),
        )
        self.assertThat(mock_check_call, MockCalledOnce())
        self.assertThat(mock_signal_wrapper, MockCalledOnce())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_apply_ephemeral_netplan_no_send(self):
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "netplan.yaml", netplan_yaml_content
        )
        backup_dir = os.path.join(self.config_dir, "netplan.bak")
        os.makedirs(backup_dir, exist_ok=True)
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            backup_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir, False)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        custom_networking._apply_ephemeral_netplan()

        self.assertFalse(os.path.exists(backup_dir))
        self.assertEquals(
            ephemeral_config_content,
            open(
                os.path.join(self.netplan_dir, "50-cloud-init.yaml"), "r"
            ).read(),
        )
        self.assertThat(mock_check_call, MockCalledOnce())
        self.assertThat(mock_signal_wrapper, MockNotCalled())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_apply_ephemeral_netplan_ignores_timeout_expired(self):
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_check_call.side_effect = TimeoutExpired(
            ["netplan", "apply", "--debug"], 60
        )
        mock_signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "netplan.yaml", netplan_yaml_content
        )
        backup_dir = os.path.join(self.config_dir, "netplan.bak")
        os.makedirs(backup_dir, exist_ok=True)
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            backup_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        custom_networking._apply_ephemeral_netplan()

        self.assertFalse(os.path.exists(backup_dir))
        self.assertEquals(
            ephemeral_config_content,
            open(
                os.path.join(self.netplan_dir, "50-cloud-init.yaml"), "r"
            ).read(),
        )
        self.assertThat(mock_check_call, MockCalledOnce())
        self.assertThat(mock_signal_wrapper, MockCalledOnce())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_apply_ephemeral_netplan_ignores_calledprocesserror(self):
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_check_call.side_effect = CalledProcessError(
            -1, ["netplan", "apply", "--debug"]
        )
        mock_signal_wrapper = self.patch(
            maas_run_remote_scripts, "signal_wrapper"
        )
        netplan_yaml_content = factory.make_string()
        factory.make_file(
            self.netplan_dir, "netplan.yaml", netplan_yaml_content
        )
        backup_dir = os.path.join(self.config_dir, "netplan.bak")
        os.makedirs(backup_dir, exist_ok=True)
        ephemeral_config_content = factory.make_string()
        factory.make_file(
            backup_dir, "50-cloud-init.yaml", ephemeral_config_content
        )
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_bring_down_networking = self.patch(
            custom_networking, "_bring_down_networking"
        )

        custom_networking._apply_ephemeral_netplan()

        self.assertFalse(os.path.exists(backup_dir))
        self.assertEquals(
            ephemeral_config_content,
            open(
                os.path.join(self.netplan_dir, "50-cloud-init.yaml"), "r"
            ).read(),
        )
        self.assertThat(mock_check_call, MockCalledOnce())
        self.assertThat(mock_signal_wrapper, MockCalledOnce())
        self.assertThat(
            self.mock_get_interfaces, MockCalledOnceWith(clear_cache=True)
        )
        self.assertThat(mock_bring_down_networking, MockCalledOnce())

    def test_exit_does_nothing_not_applying_config(self):
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=False
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_apply_ephemeral_netplan = self.patch(
            custom_networking, "_apply_ephemeral_netplan"
        )

        custom_networking.__exit__(None, None, None)

        self.assertThat(mock_apply_ephemeral_netplan, MockNotCalled())

    def test_exit_applies_ephemeral_netplan(self):
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)
        mock_apply_ephemeral_netplan = self.patch(
            custom_networking, "_apply_ephemeral_netplan"
        )

        custom_networking.__exit__(None, None, None)

        self.assertThat(mock_apply_ephemeral_netplan, MockCalledOnce())

    def test_bring_down_networking(self):
        virtual_devs = [factory.make_name("vdev") for _ in range(3)]
        physical_devs = [factory.make_name("pdev") for _ in range(3)]
        mock_listdir = self.patch(maas_run_remote_scripts.os, "listdir")
        mock_listdir.side_effect = (
            ["lo"] + virtual_devs,
            ["lo"] + physical_devs,
        )
        mock_isfile = self.patch(maas_run_remote_scripts.os.path, "isfile")
        mock_isfile.return_value = True
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)

        custom_networking._bring_down_networking()

        self.assertThat(
            mock_check_call,
            MockCallsMatch(
                *[
                    call(["ip", "link", "delete", dev], timeout=60)
                    for dev in virtual_devs
                ],
                *[
                    call(["ip", "link", "set", "down", dev], timeout=60)
                    for dev in physical_devs
                ]
            ),
        )

    def test_bring_down_networking_ignores_non_interfaces(self):
        virtual_devs = [factory.make_name("vdev") for _ in range(3)]
        physical_devs = [factory.make_name("pdev") for _ in range(3)]
        mock_listdir = self.patch(maas_run_remote_scripts.os, "listdir")
        mock_listdir.side_effect = (
            ["lo"] + virtual_devs,
            ["lo"] + physical_devs,
        )
        mock_isfile = self.patch(maas_run_remote_scripts.os.path, "isfile")
        mock_isfile.return_value = False
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)

        custom_networking._bring_down_networking()

        self.assertThat(mock_check_call, MockNotCalled())

    def test_bring_down_networking_ignores_errors(self):
        virtual_devs = [factory.make_name("vdev") for _ in range(3)]
        physical_devs = [factory.make_name("pdev") for _ in range(3)]
        mock_listdir = self.patch(maas_run_remote_scripts.os, "listdir")
        mock_listdir.side_effect = (
            ["lo"] + virtual_devs,
            ["lo"] + physical_devs,
        )
        mock_isfile = self.patch(maas_run_remote_scripts.os.path, "isfile")
        mock_isfile.return_value = True
        mock_check_call = self.patch(maas_run_remote_scripts, "check_call")
        mock_check_call.side_effect = Exception()
        scripts = make_scripts(
            scripts_dir=self.base_dir, apply_configured_networking=True
        )
        custom_networking = CustomNetworking(scripts, self.config_dir)

        custom_networking._bring_down_networking()

        self.assertThat(
            mock_check_call,
            MockCallsMatch(
                *[
                    call(["ip", "link", "delete", dev], timeout=60)
                    for dev in virtual_devs
                ],
                *[
                    call(["ip", "link", "set", "down", dev], timeout=60)
                    for dev in physical_devs
                ]
            ),
        )


class TestParseParameters(MAASTestCase):
    def test_get_block_devices(self):
        expected_blockdevs = [
            {
                "NAME": factory.make_name("NAME"),
                "MODEL": factory.make_name("MODEL"),
                "SERIAL": factory.make_name("SERIAL"),
            }
            for _ in range(3)
        ]
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        mock_check_output.return_value = "".join(
            [
                'NAME="{NAME}" MODEL="{MODEL}" SERIAL="{SERIAL}"\n'.format(
                    **blockdev
                )
                for blockdev in expected_blockdevs
            ]
        ).encode()
        maas_run_remote_scripts._block_devices = None

        self.assertItemsEqual(expected_blockdevs, get_block_devices())

    def test_get_block_devices_cached(self):
        block_devices = factory.make_name("block_devices")
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        maas_run_remote_scripts._block_devices = block_devices

        self.assertItemsEqual(block_devices, get_block_devices())
        self.assertThat(mock_check_output, MockNotCalled())

    def test_get_block_devices_cached_error(self):
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        maas_run_remote_scripts._block_devices = KeyError()

        self.assertRaises(KeyError, get_block_devices)
        self.assertThat(mock_check_output, MockNotCalled())

    def test_get_block_devices_raises_timeout_keyerror(self):
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        mock_check_output.side_effect = TimeoutExpired(
            [factory.make_name("arg") for _ in range(3)], 60
        )

        self.assertRaises(KeyError, get_block_devices)

    def test_get_block_devices_raises_calledprocess_keyerror(self):
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        mock_check_output.side_effect = CalledProcessError(
            -1, [factory.make_name("arg") for _ in range(3)]
        )

        self.assertRaises(KeyError, get_block_devices)

    def test_get_interfaces(self):
        maas_run_remote_scripts._interfaces = None
        netplan_dir = self.useFixture(TempDirectory()).path
        maas_run_remote_scripts.NETPLAN_DIR = netplan_dir
        # Bonds and bridges copy the MAC address of the first physical
        # interface by default.
        br0_mac = bond0_mac = eth0_mac = factory.make_mac_address()
        eth1_mac = factory.make_mac_address()
        eth2_mac = factory.make_mac_address()
        # Normally all configuration is stored in one config file but netplan
        # supports loading multiple. Verify maas-run-remote-scripts will read
        # from multiple.
        with open(os.path.join(netplan_dir, "interfaces.yaml"), "w") as f:
            f.write(
                yaml.safe_dump(
                    {
                        "network": {
                            "version": 2,
                            "ethernets": {
                                "eth0": {"match": {"macaddress": eth0_mac}},
                                "eth1": {"match": {"macaddress": eth1_mac}},
                                "eth2": {
                                    "match": {"macaddress": eth2_mac},
                                    random.choice(["dhcp4", "dhcp6"]): True,
                                },
                            },
                        }
                    },
                    default_flow_style=False,
                )
            )
        with open(os.path.join(netplan_dir, "bonds.yaml"), "w") as f:
            f.write(
                yaml.safe_dump(
                    {
                        "network": {
                            "version": 2,
                            "bonds": {
                                "bond0": {
                                    "interfaces": ["eth0", "eth1"],
                                    "macaddress": bond0_mac,
                                }
                            },
                        }
                    },
                    default_flow_style=False,
                )
            )
        with open(os.path.join(netplan_dir, "bridges.yaml"), "w") as f:
            f.write(
                yaml.safe_dump(
                    {
                        "network": {
                            "version": 2,
                            "bridges": {
                                "br0": {
                                    "addresses": [
                                        factory.make_ip_address(),
                                        factory.make_ip_address(),
                                    ],
                                    "interfaces": ["bond0"],
                                    "macaddress": br0_mac,
                                    "set-name": "bridge0",
                                }
                            },
                        }
                    },
                    default_flow_style=False,
                )
            )
        self.patch(maas_run_remote_scripts.os, "listdir").side_effect = (
            ["interfaces.yaml", "bonds.yaml", "bridges.yaml"],
            # Simulate interfaces taking a bit to come up to verify LP:1838114
            # work around.
            ["lo", "eth0", "eth1", "eth2"],
            ["lo", "eth0", "eth1", "eth2", "bond0", "bridge0"],
        )
        mock_sleep = self.patch(maas_run_remote_scripts.time, "sleep")

        self.assertDictEqual(
            {br0_mac: "bridge0", eth2_mac: "eth2"}, get_interfaces()
        )
        # This should only be called once but sometimes unittest catches
        # sleeps from itself which cause the lander to fail.
        self.assertThat(mock_sleep, MockAnyCall(0.1))

    def test_get_interfaces_cached(self):
        interfaces = {factory.make_mac_address(): factory.make_name("dev")}
        maas_run_remote_scripts._interfaces = interfaces
        mock_listdir = self.patch(maas_run_remote_scripts.os, "listdir")

        self.assertDictEqual(interfaces, get_interfaces())
        self.assertThat(mock_listdir, MockNotCalled())

    def test_parse_parameters(self):
        scripts_dir = factory.make_name("scripts_dir")
        script = {
            "path": os.path.join("path_to", factory.make_name("script_name")),
            "parameters": {
                "runtime": {
                    "type": "runtime",
                    "value": random.randint(0, 1000),
                },
                "url": {"type": "url", "value": factory.make_url()},
                "storage_virtio": {
                    "type": "storage",
                    "value": {
                        "name": factory.make_name("name"),
                        "model": "",
                        "serial": "",
                        "id_path": "/dev/%s" % factory.make_name("id_path"),
                    },
                },
                "storage": {
                    "type": "storage",
                    "value": {
                        "name": factory.make_name("name"),
                        "model": factory.make_name("model"),
                        "serial": factory.make_name("serial"),
                        "id_path": "/dev/%s" % factory.make_name("id_path"),
                    },
                },
                "interface_virtio": {
                    "type": "interface",
                    "value": {
                        "name": factory.make_name("name"),
                        "mac_address": factory.make_mac_address(),
                        "vendor": factory.make_name("vendor"),
                        "product": factory.make_name("product"),
                    },
                },
                "interface": {
                    "type": "interface",
                    "value": {
                        "name": factory.make_name("name"),
                        "mac_address": factory.make_mac_address(),
                        "vendor": factory.make_name("vendor"),
                        "product": factory.make_name("product"),
                    },
                },
            },
        }
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        mock_check_output.return_value = "".join(
            [
                'NAME="{name}" MODEL="{model}" SERIAL="{serial}"\n'.format(
                    **param["value"]
                )
                for param_name, param in script["parameters"].items()
                if "storage" in param_name
            ]
        ).encode()
        maas_run_remote_scripts._block_devices = None
        maas_run_remote_scripts._interfaces = {
            param["value"]["mac_address"]: param["value"]["name"]
            for param in script["parameters"].values()
            if param["type"] == "interface"
        }

        self.assertItemsEqual(
            [
                os.path.join(scripts_dir, script["path"]),
                "--runtime=%s" % script["parameters"]["runtime"]["value"],
                "--url=%s" % script["parameters"]["url"]["value"],
                "--storage=%s"
                % script["parameters"]["storage_virtio"]["value"]["id_path"],
                "--storage=/dev/%s"
                % script["parameters"]["storage"]["value"]["name"],
                "--interface=%s"
                % script["parameters"]["interface_virtio"]["value"]["name"],
                "--interface=%s"
                % script["parameters"]["interface"]["value"]["name"],
            ],
            parse_parameters(script, scripts_dir),
        )

    def test_parse_parameters_argument_format(self):
        scripts_dir = factory.make_name("scripts_dir")
        script = {
            "path": os.path.join("path_to", factory.make_name("script_name")),
            "parameters": {
                "runtime": {
                    "type": "runtime",
                    "value": random.randint(0, 1000),
                    "argument_format": "--foo --timeout {input}",
                },
                "url": {
                    "type": "url",
                    "value": factory.make_url(),
                    "argument_format": "--blah {input}",
                },
                "storage": {
                    "type": "storage",
                    "value": {
                        "name": factory.make_name("name"),
                        "model": factory.make_name("model"),
                        "serial": factory.make_name("serial"),
                        "id_path": "/dev/%s" % factory.make_name("id_path"),
                    },
                    "argument_format": (
                        "--bar {name} {model} {serial} {path} {input}"
                    ),
                },
                "interface": {
                    "type": "interface",
                    "value": {
                        "name": factory.make_name("name"),
                        "mac_address": factory.make_mac_address(),
                        "vendor": factory.make_name("vendor"),
                        "product": factory.make_name("product"),
                    },
                    "argument_format": (
                        "--blah {name} {mac_address} {vendor} {product}"
                    ),
                },
            },
        }
        mock_check_output = self.patch(maas_run_remote_scripts, "check_output")
        mock_check_output.return_value = "".join(
            [
                'NAME="{name}" MODEL="{model}" SERIAL="{serial}"\n'.format(
                    **param["value"]
                )
                for param_name, param in script["parameters"].items()
                if "storage" in param_name
            ]
        ).encode()
        maas_run_remote_scripts._block_devices = None
        maas_run_remote_scripts._interfaces = {
            param["value"]["mac_address"]: param["value"]["name"]
            for param in script["parameters"].values()
            if param["type"] == "interface"
        }

        self.assertItemsEqual(
            [
                os.path.join(scripts_dir, script["path"]),
                "--foo",
                "--timeout",
                str(script["parameters"]["runtime"]["value"]),
                "--blah",
                script["parameters"]["url"]["value"],
                "--bar",
                script["parameters"]["storage"]["value"]["name"],
                script["parameters"]["storage"]["value"]["model"],
                script["parameters"]["storage"]["value"]["serial"],
                "/dev/%s" % script["parameters"]["storage"]["value"]["name"],
                "/dev/%s" % script["parameters"]["storage"]["value"]["name"],
                "--blah",
                script["parameters"]["interface"]["value"]["name"],
                script["parameters"]["interface"]["value"]["mac_address"],
                script["parameters"]["interface"]["value"]["vendor"],
                script["parameters"]["interface"]["value"]["product"],
            ],
            parse_parameters(script, scripts_dir),
        )

    def test_parse_parameters_storage_value_all_raises_keyerror(self):
        scripts_dir = factory.make_name("scripts_dir")
        script = {
            "path": os.path.join("path_to", factory.make_name("script_name")),
            "parameters": {"storage": {"type": "storage", "value": "all"}},
        }

        self.assertRaises(KeyError, parse_parameters, script, scripts_dir)

    def test_parse_parameters_interface_value_all_raises_keyerror(self):
        scripts_dir = factory.make_name("scripts_dir")
        script = {
            "path": os.path.join("path_to", factory.make_name("script_name")),
            "parameters": {"interface": {"type": "interface", "value": "all"}},
        }

        self.assertRaises(KeyError, parse_parameters, script, scripts_dir)


class TestCheckLinkConnected(MAASTestCase):
    def test_only_runs_with_network_script(self):
        script = make_script(
            hardware_type=random.randint(0, 3),
            apply_configured_networking=True,
        )
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")

        _check_link_connected(script)

        self.assertThat(mock_join, MockNotCalled())

    def test_only_runs_when_network_settings_applied(self):
        script = make_script(
            hardware_type=4, apply_configured_networking=False
        )
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")

        _check_link_connected(script)

        self.assertThat(mock_join, MockNotCalled())

    def test_only_runs_with_interface_param(self):
        script = make_script(hardware_type=4, apply_configured_networking=True)
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")

        _check_link_connected(script)

        self.assertThat(mock_join, MockNotCalled())

    def test_does_nothing_when_interface_is_not_found(self):
        script = make_script(hardware_type=4, apply_configured_networking=True)
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {
            factory.make_mac_address(): "eth1"
        }
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")

        _check_link_connected(script)

        self.assertThat(mock_join, MockNotCalled())

    def test_does_nothing_when_link_is_up(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("up\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path
        mock_exists = self.patch(maas_run_remote_scripts.os.path, "exists")

        _check_link_connected(script)

        self.assertThat(mock_exists, MockNotCalled())

    def test_check_link_connected_reports_link_down_on_failure(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                {"link_connected": False}, yaml.safe_load(f.read())
            )

    def test_check_link_connected_does_nothing_on_pass(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 0
        os.remove(script["result_path"])
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        self.assertFalse(os.path.exists(script["result_path"]))

    def test_check_link_connected_does_nothing_on_bad_yaml_file(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        bad_yaml = factory.make_bytes()
        with open(script["result_path"], "wb") as f:
            f.write(bad_yaml)
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "rb") as f:
            self.assertEqual(bad_yaml, f.read())

    def test_check_link_connected_handles_empty_result_file(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        with open(script["result_path"], "w") as f:
            f.write("")
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                {"link_connected": False}, yaml.safe_load(f.read())
            )

    def test_check_link_connected_does_nothing_with_nondict_result(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        nondict_result = factory.make_string()
        with open(script["result_path"], "w") as f:
            f.write(nondict_result)
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertEqual(nondict_result, f.read())

    def test_check_link_connected_does_nothing_when_link_connected_def(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        with open(script["result_path"], "w") as f:
            f.write(yaml.safe_dump({"link_connected": True}))
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                {"link_connected": True}, yaml.safe_load(f.read())
            )

    def test_check_link_connected_does_nothing_when_yaml_passed(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        os.remove(script["result_path"])
        with open(script["result_path"], "w") as f:
            f.write(yaml.safe_dump({"status": "passed"}))
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                {"status": "passed"}, yaml.safe_load(f.read())
            )

    def test_check_link_connected_does_nothing_when_script_passed(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 0
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                yaml.safe_load(script["result"]), yaml.safe_load(f.read())
            )

    def test_check_link_connected_appends_yaml(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(
            scripts_dir=scripts_dir,
            hardware_type=4,
            apply_configured_networking=True,
        )
        script["exit_status"] = 1
        mac_address = factory.make_mac_address()
        maas_run_remote_scripts._interfaces = {mac_address: "eth0"}
        script["parameters"] = {
            "interface": {
                "type": "interface",
                "value": {"name": "eth0", "mac_address": mac_address},
            }
        }
        operstate_path = os.path.join(scripts_dir, "operstate")
        with open(operstate_path, "w") as f:
            f.write("down\n")
        mock_join = self.patch(maas_run_remote_scripts.os.path, "join")
        mock_join.return_value = operstate_path

        _check_link_connected(script)

        with open(script["result_path"], "r") as f:
            self.assertDictEqual(
                {"link_connected": False, **yaml.safe_load(script["result"])},
                yaml.safe_load(f.read()),
            )


class TestRunScript(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.mock_output_and_send = self.patch(
            maas_run_remote_scripts, "output_and_send"
        )
        self.mock_capture_script_output = self.patch(
            maas_run_remote_scripts, "capture_script_output"
        )
        self.mock_check_link_connected = self.patch(
            maas_run_remote_scripts, "_check_link_connected"
        )
        self.args = {"status": "WORKING", "send_result": True}
        self.patch(maas_run_remote_scripts.sys.stdout, "write")

    def test_run_script(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)

        run_script(script, scripts_dir)

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Finished %s: None" % script["msg_name"],
                    exit_status=None,
                    files={
                        script["combined_name"]: script["combined"].encode(),
                        script["stdout_name"]: script["stdout"].encode(),
                        script["stderr_name"]: script["stderr"].encode(),
                        script["result_name"]: script["result"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_capture_script_output,
            MockCalledOnceWith(
                ANY,
                script["combined_path"],
                script["stdout_path"],
                script["stderr_path"],
                script["timeout_seconds"],
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_sets_env(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        mock_popen = self.patch(maas_run_remote_scripts, "Popen")

        run_script(script, scripts_dir)

        env = mock_popen.call_args[1]["env"]
        self.assertEquals(script["combined_path"], env["OUTPUT_COMBINED_PATH"])
        self.assertEquals(script["stdout_path"], env["OUTPUT_STDOUT_PATH"])
        self.assertEquals(script["stderr_path"], env["OUTPUT_STDERR_PATH"])
        self.assertEquals(script["result_path"], env["RESULT_PATH"])
        self.assertEquals(script["download_path"], env["DOWNLOAD_PATH"])
        self.assertEquals(str(script["timeout_seconds"]), env["RUNTIME"])
        self.assertEquals(str(script["has_started"]), env["HAS_STARTED"])
        self.assertIn("PATH", env)
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_only_sends_result_when_avail(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        os.remove(script["result_path"])

        run_script(script, scripts_dir)

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Finished %s: None" % script["msg_name"],
                    exit_status=None,
                    files={
                        script["combined_name"]: script["combined"].encode(),
                        script["stdout_name"]: script["stdout"].encode(),
                        script["stderr_name"]: script["stderr"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_capture_script_output,
            MockCalledOnceWith(
                ANY,
                script["combined_path"],
                script["stdout_path"],
                script["stderr_path"],
                script["timeout_seconds"],
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_uses_timeout_from_parameter(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        script["parameters"] = {
            "runtime": {"type": "runtime", "value": random.randint(0, 1000)}
        }

        run_script(script, scripts_dir)

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Finished %s: None" % script["msg_name"],
                    exit_status=None,
                    files={
                        script["combined_name"]: script["combined"].encode(),
                        script["stdout_name"]: script["stdout"].encode(),
                        script["stderr_name"]: script["stderr"].encode(),
                        script["result_name"]: script["result"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_capture_script_output,
            MockCalledOnceWith(
                ANY,
                script["combined_path"],
                script["stdout_path"],
                script["stderr_path"],
                script["parameters"]["runtime"]["value"],
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_errors_with_bad_param(self):
        fake_block_devices = [
            {
                "MODEL": factory.make_name("model"),
                "SERIAL": factory.make_name("serial"),
            }
            for _ in range(3)
        ]
        fake_interfaces = [
            {factory.make_mac_address(): "eth%s" % i for i in range(3)}
        ]
        mock_get_block_devices = self.patch(
            maas_run_remote_scripts, "get_block_devices"
        )
        mock_get_interfaces = self.patch(
            maas_run_remote_scripts, "get_interfaces"
        )
        mock_get_interfaces.return_value = fake_interfaces
        mock_get_block_devices.return_value = fake_block_devices
        testing_block_device_model = factory.make_name("model")
        testing_block_device_serial = factory.make_name("serial")
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        script["parameters"] = {
            "storage": {
                "type": "storage",
                "argument_format": "{bad}",
                "value": {
                    "model": testing_block_device_model,
                    "serial": testing_block_device_serial,
                },
            }
        }

        self.assertFalse(run_script(script, scripts_dir))

        expected_output = (
            "Unable to run '%s': Storage device '%s' with serial '%s' not "
            "found!\n\n"
            "This indicates the storage device has been removed or "
            "the OS is unable to find it due to a hardware failure. "
            "Please re-commission this node to re-discover the "
            "storage devices, or delete this device manually.\n\n"
            "Given parameters:\n%s\n\n"
            "Discovered storage devices:\n%s\n"
            "Discovered interfaces:\n%s\n"
            % (
                script["name"],
                testing_block_device_model,
                testing_block_device_serial,
                str(script["parameters"]),
                str(fake_block_devices),
                str(fake_interfaces),
            )
        )
        expected_output = expected_output.encode()
        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Failed to execute %s: 2" % script["msg_name"],
                    exit_status=2,
                    files={
                        script["combined_name"]: expected_output,
                        script["stderr_name"]: expected_output,
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(self.mock_check_link_connected, MockNotCalled())

    def test_run_script_errors_bad_params_on_unexecutable_script(self):
        # Regression test for LP:1669246
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        self.mock_capture_script_output.side_effect = OSError(
            8, "Exec format error"
        )

        self.assertFalse(run_script(script, scripts_dir))

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Failed to execute %s: 8" % script["msg_name"],
                    exit_status=8,
                    files={
                        script[
                            "combined_name"
                        ]: b"[Errno 8] Exec format error",
                        script["stderr_name"]: b"[Errno 8] Exec format error",
                        script["result_name"]: script["result"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_errors_bad_params_on_unexecutable_script_no_errno(
        self,
    ):
        # Regression test for LP:1669246
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        self.mock_capture_script_output.side_effect = OSError()

        self.assertFalse(run_script(script, scripts_dir))

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Failed to execute %s: 2" % script["msg_name"],
                    exit_status=2,
                    files={
                        script["combined_name"]: b"Unable to execute script",
                        script["stderr_name"]: b"Unable to execute script",
                        script["result_name"]: script["result"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_errors_bad_params_on_unexecutable_script_baderrno(
        self,
    ):
        # Regression test for LP:1669246
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        self.mock_capture_script_output.side_effect = OSError(
            0, "Exec format error"
        )

        self.assertFalse(run_script(script, scripts_dir))

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    **script["args"],
                    **self.args
                ),
                call(
                    "Failed to execute %s: 2" % script["msg_name"],
                    exit_status=2,
                    files={
                        script[
                            "combined_name"
                        ]: b"[Errno 0] Exec format error",
                        script["stderr_name"]: b"[Errno 0] Exec format error",
                        script["result_name"]: script["result"].encode(),
                    },
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )

    def test_run_script_timed_out_script(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        script = make_script(scripts_dir=scripts_dir)
        self.mock_capture_script_output.side_effect = TimeoutExpired(
            [factory.make_name("arg") for _ in range(3)],
            script["timeout_seconds"],
        )
        self.args.pop("status")

        self.assertFalse(run_script(script, scripts_dir))

        self.assertThat(
            self.mock_output_and_send,
            MockCallsMatch(
                call(
                    "Starting %s" % script["msg_name"],
                    status="WORKING",
                    **script["args"],
                    **self.args
                ),
                call(
                    "Timeout(%s) expired on %s"
                    % (
                        str(timedelta(seconds=script["timeout_seconds"])),
                        script["msg_name"],
                    ),
                    exit_status=124,
                    files={
                        script["combined_name"]: script["combined"].encode(),
                        script["stdout_name"]: script["stdout"].encode(),
                        script["stderr_name"]: script["stderr"].encode(),
                        script["result_name"]: script["result"].encode(),
                    },
                    status="TIMEDOUT",
                    **script["args"],
                    **self.args
                ),
            ),
        )
        self.assertThat(
            self.mock_check_link_connected, MockCalledOnceWith(script)
        )


class TestRunScripts(MAASTestCase):
    def test_run_scripts(self):
        mock_run_serial_scripts = self.patch(
            maas_run_remote_scripts, "run_serial_scripts"
        )
        mock_run_instance_scripts = self.patch(
            maas_run_remote_scripts, "run_instance_scripts"
        )
        mock_run_parallel_scripts = self.patch(
            maas_run_remote_scripts, "run_parallel_scripts"
        )
        single_thread = make_scripts(instance=False, parallel=0)
        instance_thread = [make_scripts(parallel=1) for _ in range(3)]
        any_thread = make_scripts(instance=False, parallel=2)
        scripts = copy.deepcopy(single_thread)
        for instance_thread_group in instance_thread:
            scripts += copy.deepcopy(instance_thread_group)
        scripts += copy.deepcopy(any_thread)
        url = factory.make_url()
        creds = factory.make_name("creds")
        scripts_dir = factory.make_name("scripts_dir")
        out_dir = os.path.join(scripts_dir, "out")

        run_scripts(url, creds, scripts_dir, out_dir, scripts)

        serial_scripts = []
        instance_scripts = []
        parallel_scripts = []
        for script in sorted(
            scripts,
            key=lambda i: (
                99 if i["hardware_type"] == 0 else i["hardware_type"],
                i["name"],
            ),
        ):
            if script["parallel"] == 0:
                serial_scripts.append(script)
            elif script["parallel"] == 1:
                instance_scripts.append(script)
            elif script["parallel"] == 2:
                parallel_scripts.append(script)

        self.assertThat(
            mock_run_serial_scripts,
            MockCalledOnceWith(serial_scripts, scripts_dir, ANY, True),
        )
        self.assertThat(
            mock_run_instance_scripts,
            MockCalledOnceWith(instance_scripts, scripts_dir, ANY, True),
        )
        self.assertThat(
            mock_run_parallel_scripts,
            MockCalledOnceWith(parallel_scripts, scripts_dir, ANY, True),
        )

    def test_run_scripts_adds_data(self):
        scripts_dir = factory.make_name("scripts_dir")
        out_dir = os.path.join(scripts_dir, "out")
        self.patch(maas_run_remote_scripts, "install_dependencies")
        self.patch(maas_run_remote_scripts, "run_script")
        url = factory.make_url()
        creds = factory.make_name("creds")
        script = make_script(scripts_dir=scripts_dir)
        script.pop("result", None)
        script.pop("combined", None)
        script.pop("stderr", None)
        script.pop("stdout", None)
        script["args"] = {
            "url": url,
            "creds": creds,
            "script_result_id": script["script_result_id"],
            "script_version_id": script["script_version_id"],
        }
        scripts = [
            {
                "name": script["name"],
                "path": script["path"],
                "script_result_id": script["script_result_id"],
                "script_version_id": script["script_version_id"],
                "timeout_seconds": script["timeout_seconds"],
                "parallel": script["parallel"],
                "hardware_type": script["hardware_type"],
                "has_started": script["has_started"],
                "apply_configured_networking": script[
                    "apply_configured_networking"
                ],
                "args": script["args"],
            }
        ]
        run_scripts(url, creds, scripts_dir, out_dir, scripts)
        scripts[0].pop("thread", None)
        self.assertDictEqual(script, scripts[0])


class TestRunScriptsFromMetadata(MAASTestCase):
    def setUp(self):
        super().setUp()
        self.mock_output_and_send = self.patch(
            maas_run_remote_scripts, "output_and_send"
        )
        self.mock_signal = self.patch(maas_run_remote_scripts, "signal")
        self.mock_run_scripts = self.patch(
            maas_run_remote_scripts, "run_scripts"
        )
        self.patch(maas_run_remote_scripts.sys.stdout, "write")

    def make_index_json(
        self,
        scripts_dir,
        with_commissioning=True,
        with_testing=True,
        commissioning_scripts=None,
        testing_scripts=None,
    ):
        index_json = {}
        if with_commissioning:
            if commissioning_scripts is None:
                index_json["commissioning_scripts"] = make_scripts()
            else:
                index_json["commissioning_scripts"] = commissioning_scripts
        if with_testing:
            if testing_scripts is None:
                index_json["testing_scripts"] = make_scripts()
            else:
                index_json["testing_scripts"] = testing_scripts
        with open(os.path.join(scripts_dir, "index.json"), "w") as f:
            f.write(json.dumps({"1.0": index_json}))
        return index_json

    def mock_download_and_extract_tar(self, url, creds, scripts_dir):
        """Simulate redownloading a scripts tarball after finishing commiss."""
        index_path = os.path.join(scripts_dir, "index.json")
        with open(index_path, "r") as f:
            index_json = json.loads(f.read())
        index_json["1.0"].pop("commissioning_scripts", None)
        os.remove(index_path)
        with open(index_path, "w") as f:
            f.write(json.dumps(index_json))
        return True

    def test_run_scripts_from_metadata(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        self.mock_run_scripts.return_value = 0
        index_json = self.make_index_json(scripts_dir)
        mock_download_and_extract_tar = self.patch(
            maas_run_remote_scripts, "download_and_extract_tar"
        )
        mock_download_and_extract_tar.side_effect = (
            self.mock_download_and_extract_tar
        )

        # Don't need to give the url, creds, or out_dir as we're not running
        # the scripts and sending the results.
        run_scripts_from_metadata(None, None, scripts_dir, None)

        self.assertThat(
            self.mock_run_scripts,
            MockAnyCall(
                None,
                None,
                scripts_dir,
                None,
                index_json["commissioning_scripts"],
                True,
            ),
        )
        self.assertThat(
            self.mock_run_scripts,
            MockAnyCall(
                None,
                None,
                scripts_dir,
                None,
                index_json["testing_scripts"],
                True,
            ),
        )
        self.assertThat(self.mock_signal, MockAnyCall(None, None, "TESTING"))
        self.assertThat(
            mock_download_and_extract_tar,
            MockCalledOnceWith("Nonemaas-scripts", None, scripts_dir),
        )

    def test_run_scripts_from_metadata_doesnt_run_tests_on_commiss_fail(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        fail_count = random.randint(1, 100)
        self.mock_run_scripts.return_value = fail_count
        index_json = self.make_index_json(scripts_dir)

        # Don't need to give the url, creds, or out_dir as we're not running
        # the scripts and sending the results.
        run_scripts_from_metadata(None, None, scripts_dir, None)

        self.assertThat(
            self.mock_run_scripts,
            MockCalledOnceWith(
                None,
                None,
                scripts_dir,
                None,
                index_json["commissioning_scripts"],
                True,
            ),
        )
        self.assertThat(self.mock_signal, MockNotCalled())
        self.assertThat(
            self.mock_output_and_send,
            MockCalledOnceWith(
                "%s commissioning scripts failed to run" % fail_count,
                True,
                None,
                None,
                "FAILED",
            ),
        )

    def test_run_scripts_from_metadata_redownloads_after_commiss(self):
        scripts_dir = self.useFixture(TempDirectory()).path
        self.mock_run_scripts.return_value = 0
        testing_scripts = make_scripts()
        testing_scripts[0]["parameters"] = {"storage": {"type": "storage"}}
        mock_download_and_extract_tar = self.patch(
            maas_run_remote_scripts, "download_and_extract_tar"
        )
        simple_dl_and_extract = lambda *args, **kwargs: self.make_index_json(
            scripts_dir,
            with_commissioning=False,
            testing_scripts=testing_scripts,
        )
        mock_download_and_extract_tar.side_effect = simple_dl_and_extract
        index_json = self.make_index_json(
            scripts_dir, testing_scripts=testing_scripts
        )

        # Don't need to give the url, creds, or out_dir as we're not running
        # the scripts and sending the results.
        run_scripts_from_metadata(None, None, scripts_dir, None)

        self.assertThat(
            self.mock_run_scripts,
            MockAnyCall(
                None,
                None,
                scripts_dir,
                None,
                index_json["commissioning_scripts"],
                True,
            ),
        )
        self.assertThat(self.mock_signal, MockAnyCall(None, None, "TESTING"))
        self.assertThat(
            mock_download_and_extract_tar,
            MockCalledOnceWith("Nonemaas-scripts", None, scripts_dir),
        )
        self.assertThat(
            self.mock_run_scripts,
            MockAnyCall(
                None,
                None,
                scripts_dir,
                None,
                index_json["testing_scripts"],
                True,
            ),
        )


class TestMaasRunRemoteScripts(MAASTestCase):
    def test_download_and_extract_tar(self):
        self.patch(maas_run_remote_scripts.sys.stdout, "write")
        scripts_dir = self.useFixture(TempDirectory()).path
        binary = BytesIO()
        file_content = factory.make_bytes()
        with tarfile.open(mode="w", fileobj=binary) as tar:
            tarinfo = tarfile.TarInfo(name="test-file")
            tarinfo.size = len(file_content)
            tarinfo.mode = 0o755
            tar.addfile(tarinfo, BytesIO(file_content))
        mock_geturl = self.patch(maas_run_remote_scripts, "geturl")
        mm = MagicMock()
        mm.status = 200
        mm.read.return_value = binary.getvalue()
        mock_geturl.return_value = mm

        # geturl is mocked out so we don't need to give a url or creds.
        self.assertTrue(download_and_extract_tar(None, None, scripts_dir))

        written_file_content = open(
            os.path.join(scripts_dir, "test-file"), "rb"
        ).read()
        self.assertEquals(file_content, written_file_content)

    def test_download_and_extract_tar_returns_false_on_no_content(self):
        self.patch(maas_run_remote_scripts.sys.stdout, "write")
        scripts_dir = self.useFixture(TempDirectory()).path
        mock_geturl = self.patch(maas_run_remote_scripts, "geturl")
        mm = MagicMock()
        mm.status = int(http.client.NO_CONTENT)
        mm.read.return_value = b"No content"
        mock_geturl.return_value = mm

        # geturl is mocked out so we don't need to give a url or creds.
        self.assertFalse(download_and_extract_tar(None, None, scripts_dir))

    def test_heartbeat(self):
        mock_signal = self.patch(maas_run_remote_scripts, "signal")
        url = factory.make_url()
        creds = factory.make_name("creds")
        heart_beat = maas_run_remote_scripts.HeartBeat(url, creds)
        start_time = time.time()
        heart_beat.start()
        heart_beat.stop()
        self.assertLess(time.time() - start_time, 1)
        self.assertThat(mock_signal, MockCalledOnceWith(url, creds, "WORKING"))

    def test_heartbeat_with_long_sleep(self):
        mock_signal = self.patch(maas_run_remote_scripts, "signal")
        self.patch(maas_run_remote_scripts.time, "monotonic").side_effect = [
            time.monotonic(),
            time.monotonic(),
            time.monotonic() + 500,
        ]
        url = factory.make_url()
        creds = factory.make_name("creds")
        heart_beat = maas_run_remote_scripts.HeartBeat(url, creds)
        start_time = time.time()
        heart_beat.start()
        heart_beat.stop()
        self.assertLess(time.time() - start_time, 1)
        self.assertThat(mock_signal, MockCalledOnceWith(url, creds, "WORKING"))

    def test_main_signals_success(self):
        self.patch(
            maas_run_remote_scripts.argparse.ArgumentParser, "parse_args"
        )
        self.patch(maas_run_remote_scripts, "read_config")
        self.patch(maas_run_remote_scripts, "os")
        self.patch(maas_run_remote_scripts, "open")
        self.patch(
            maas_run_remote_scripts, "download_and_extract_tar"
        ).return_value = True
        self.patch(
            maas_run_remote_scripts, "run_scripts_from_metadata"
        ).return_value = 0
        self.patch(maas_run_remote_scripts, "signal")
        mock_output_and_send = self.patch(
            maas_run_remote_scripts, "output_and_send"
        )

        maas_run_remote_scripts.main()

        self.assertThat(
            mock_output_and_send,
            MockCalledOnceWith(
                "All scripts successfully ran", ANY, ANY, ANY, "OK"
            ),
        )

    def test_main_signals_failure(self):
        failures = random.randint(1, 100)
        self.patch(
            maas_run_remote_scripts.argparse.ArgumentParser, "parse_args"
        )
        self.patch(maas_run_remote_scripts, "read_config")
        self.patch(maas_run_remote_scripts, "os")
        self.patch(maas_run_remote_scripts, "open")
        self.patch(
            maas_run_remote_scripts, "download_and_extract_tar"
        ).return_value = True
        self.patch(
            maas_run_remote_scripts, "run_scripts_from_metadata"
        ).return_value = failures
        self.patch(maas_run_remote_scripts, "signal")
        mock_output_and_send = self.patch(
            maas_run_remote_scripts, "output_and_send"
        )

        maas_run_remote_scripts.main()

        self.assertThat(
            mock_output_and_send,
            MockCalledOnceWith(
                "%d test scripts failed to run" % failures,
                ANY,
                ANY,
                ANY,
                "FAILED",
            ),
        )
