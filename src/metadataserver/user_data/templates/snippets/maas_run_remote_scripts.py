#!/usr/bin/env python3
#
# maas-run-remote-scripts - Download a set of scripts from the MAAS region,
#                           execute them, and send the results back.
#
# Author: Lee Trager <lee.trager@canonical.com>
#
# Copyright (C) 2017-2019 Canonical
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import argparse
import copy
from datetime import timedelta
import http.client
from io import BytesIO
import json
import os
import re
import shlex
import shutil
from subprocess import (
    CalledProcessError,
    check_call,
    check_output,
    DEVNULL,
    PIPE,
    Popen,
    TimeoutExpired,
)
import sys
import tarfile
from threading import Event, Lock, Thread
import time
import traceback
import zipfile

import yaml


try:
    from maas_api_helper import (
        geturl,
        MD_VERSION,
        read_config,
        signal,
        SignalException,
        capture_script_output,
    )
except ImportError:
    # For running unit tests.
    from snippets.maas_api_helper import (
        geturl,
        MD_VERSION,
        read_config,
        signal,
        SignalException,
        capture_script_output,
    )


NETPLAN_DIR = "/etc/netplan"


def fail(msg):
    sys.stderr.write("FAIL: %s" % msg)
    sys.stderr.flush()
    sys.exit(1)


def signal_wrapper(*args, **kwargs):
    """Wrapper to output any SignalExceptions to STDERR."""
    try:
        signal(*args, **kwargs)
    except SignalException as e:
        fail(e.error)


def output_and_send(error, send_result=True, *args, **kwargs):
    """Output the error message to stderr and send iff send_result is True."""
    sys.stdout.write("%s\n" % error)
    sys.stdout.flush()
    if send_result:
        signal_wrapper(*args, error=error, **kwargs)


def output_and_send_scripts(
    error, scripts, send_result=True, error_is_stderr=False, *args, **kwargs
):
    """output_and_send for multiple scripts."""
    for script in scripts:
        script_error = error.format(**script)
        new_kwargs = copy.deepcopy(kwargs)
        if error_is_stderr:
            script_output = "%s\n" % script_error
            script_output = script_output.encode()
            # Write output to the filesystem to help with debug.
            for path in [script["combined_path"], script["stderr_path"]]:
                with open(path, "ab") as out:
                    out.write(script_output)
            new_kwargs["files"] = {
                script["combined_name"]: script_output,
                script["stderr_name"]: script_output,
            }
        output_and_send(
            script_error, send_result, *args, **script["args"], **new_kwargs
        )


def download_and_extract_tar(url, creds, scripts_dir):
    """Download and extract a tar from the given URL.

    The URL may contain a compressed or uncompressed tar. Returns false when
    there is no content.
    """
    sys.stdout.write(
        "Downloading and extracting %s to %s\n" % (url, scripts_dir)
    )
    sys.stdout.flush()
    ret = geturl(url, creds)
    if ret.status == int(http.client.NO_CONTENT):
        return False
    binary = BytesIO(ret.read())

    with tarfile.open(mode="r|*", fileobj=binary) as tar:
        tar.extractall(scripts_dir)

    return True


def run_and_check(
    cmd, scripts, status, send_result=True, sudo=False, failure_hook=None
):
    if sudo:
        cmd = ["sudo", "-En"] + cmd
    proc = Popen(cmd, stdin=DEVNULL, stdout=PIPE, stderr=PIPE)
    capture_script_output(
        proc,
        scripts[0]["combined_path"],
        scripts[0]["stdout_path"],
        scripts[0]["stderr_path"],
    )
    if proc.returncode != 0 and send_result:
        if failure_hook is not None:
            failure_hook()
        for script in scripts:
            args = copy.deepcopy(script["args"])
            script["exit_status"] = args["exit_status"] = proc.returncode
            args["status"] = status
            args["files"] = {
                scripts[0]["combined_name"]: open(
                    scripts[0]["combined_path"], "rb"
                ).read(),
                scripts[0]["stdout_name"]: open(
                    scripts[0]["stdout_path"], "rb"
                ).read(),
                scripts[0]["stderr_name"]: open(
                    scripts[0]["stderr_path"], "rb"
                ).read(),
            }
            output_and_send(
                "Failed installing package(s) for %s" % script["msg_name"],
                **args
            )
        return False
    else:
        return True


def _install_apt_dependencies(packages, scripts, send_result=True):
    output_and_send_scripts(
        "Installing apt packages for {msg_name}",
        scripts,
        send_result,
        status="INSTALLING",
    )
    # Check if apt-get update needs to be run
    if not os.path.exists("/var/cache/apt/pkgcache.bin"):
        if not run_and_check(
            ["apt-get", "-qy", "update"],
            scripts,
            "INSTALLING",
            send_result,
            True,
        ):
            return False
    if not run_and_check(
        ["apt-get", "-qy", "--no-install-recommends", "install"] + packages,
        scripts,
        "INSTALLING",
        send_result,
        True,
    ):
        return False

    return True


def _install_snap_dependencies(packages, scripts, send_result=True):
    output_and_send_scripts(
        "Installing snap packages for {msg_name}",
        scripts,
        send_result,
        status="INSTALLING",
    )
    for pkg in packages:
        if isinstance(pkg, str):
            cmd = ["snap", "install", pkg]
        elif isinstance(pkg, dict):
            cmd = ["snap", "install", pkg["name"]]
            if "channel" in pkg:
                cmd.append("--%s" % pkg["channel"])
            if "mode" in pkg:
                if pkg["mode"] == "classic":
                    cmd.append("--classic")
                else:
                    cmd.append("--%smode" % pkg["mode"])
        else:
            # The ScriptForm validates that each snap package should be a
            # string or dictionary. This should never happen but just
            # incase it does...
            continue
        if not run_and_check(cmd, scripts, "INSTALLING", send_result, True):
            return False

    return True


def _install_url_dependencies(packages, scripts, send_result=True):
    output_and_send_scripts(
        "Downloading and extracting URLs for {msg_name}",
        scripts,
        send_result,
        status="INSTALLING",
    )
    path_regex = re.compile("^Saving to: ['‘](?P<path>.+)['’]$", re.M)
    # Install only happens once for all scripts in a group so use the first
    # script's paths.
    download_path = scripts[0]["download_path"]
    combined_path = scripts[0]["combined_path"]
    for pkg in packages:
        # wget supports multiple protocols, proxying, proper error message,
        # handling user input without protocol information, and getting the
        # filename from the request. Shell out and capture its output
        # instead of implementing all of that here.
        if not run_and_check(
            ["wget", pkg, "-P", download_path],
            scripts,
            "INSTALLING",
            send_result,
        ):
            return False

        # Get the filename from the captured output incase the URL does not
        # include a filename. e.g the URL 'ubuntu.com' will create an
        # index.html file.
        with open(combined_path, "r") as combined:
            m = path_regex.findall(combined.read())
            if m != []:
                filename = m[-1]
            else:
                # Unable to find filename in output.
                continue

        if tarfile.is_tarfile(filename):
            with tarfile.open(filename, "r|*") as tar:
                tar.extractall(download_path)
        elif zipfile.is_zipfile(filename):
            with zipfile.ZipFile(filename, "r") as z:
                z.extractall(download_path)
        elif filename.endswith(".deb"):
            # Allow dpkg to fail incase it just needs dependencies
            # installed.
            run_and_check(
                ["dpkg", "-i", filename], scripts, "INSTALLING", False, True
            )
            if not run_and_check(
                ["apt-get", "install", "-qyf", "--no-install-recommends"],
                scripts,
                "INSTALLING",
                send_result,
                True,
            ):
                return False
        elif filename.endswith(".snap"):
            if not run_and_check(
                ["snap", filename], scripts, "INSTALLING", send_result, True
            ):
                return False

    return True


def _clean_logs(scripts):
    """Remove logs written before running a script."""
    if not scripts:
        return
    # Installing and applying network configuration only happens once for a
    # group of scripts. Logs are always stored in the first scripts path and
    # sent as the result in case of failure. If this is called installation
    # or applying network configuration was successful.
    for path in [
        scripts[0]["combined_path"],
        scripts[0]["stdout_path"],
        scripts[0]["stderr_path"],
    ]:
        if os.path.exists(path):
            os.remove(path)


def install_dependencies(scripts, send_result=True):
    """Download and install any required packaged for the script to run.

    If given a list of scripts assumes the package set is the same and signals
    installation status for all script results."""
    if scripts:
        # A group of scripts is given when instance scripts are running. In
        # this case the packages are all the same.
        packages = scripts[0].get("packages", {})
    else:
        return True
    installers = {
        "apt": _install_apt_dependencies,
        "snap": _install_snap_dependencies,
        "url": _install_url_dependencies,
    }

    for t, installer in installers.items():
        pkgs = packages.get(t)
        if pkgs is not None:
            if not installer(pkgs, scripts, send_result):
                return False

    # All went well, clean up the install logs so only script output is
    # captured.
    _clean_logs(scripts)
    return True


class CustomNetworking:
    def __init__(self, scripts, config_dir, send_result=True):
        self.scripts = scripts
        self.config_dir = config_dir
        self.backup_dir = os.path.join(config_dir, "netplan.bak")
        self.netplan_yaml = os.path.join(self.config_dir, "netplan.yaml")
        self.send_result = send_result
        if scripts:
            self.url = scripts[0]["args"]["url"]
            self.creds = scripts[0]["args"]["creds"]
            self.apply_configured_networking = scripts[0].get(
                "apply_configured_networking", False
            )
        else:
            self.url = self.creds = None
            self.apply_configured_networking = False

    def _bring_down_networking(self):
        """Bring down all networking.

        netplan applies network settings to your system but does not remove
        everything. For example if a bond configuration is applied then a
        basic DHCP configuration is applied and the bond will still be up. Even
        if IP addresses are removed from an interface they remain up. Bring
        down networking so when a new configuration is applied its in a clean
        environment.

        This method doesn't trust get_interfaces() as a user provided
        script may have changed the network configuration."""
        for path, cmd in (
            ("/sys/devices/virtual/net", ["ip", "link", "delete"]),
            ("/sys/class/net", ["ip", "link", "set", "down"]),
        ):
            for dev in os.listdir(path):
                if dev == "lo":
                    # lo should always be available and up. netplan will not
                    # reconfigure lo
                    continue
                if not os.path.isfile(os.path.join(path, dev, "address")):
                    # This isn't a device if it doesn't have an address.
                    continue
                try:
                    check_call(cmd + [dev], timeout=60)
                except Exception:
                    # This is a best effort. netplan apply will be run after
                    # this which may restore networking.
                    pass

    def _apply_ephemeral_netplan(self):
        """Apply netplan config from ephemeral boot."""
        # If there is no backup_dir the ephemeral network configuration
        # was already applied.
        if not os.path.exists(self.backup_dir):
            return

        applied_netplan_yaml = os.path.join(NETPLAN_DIR, "netplan.yaml")
        if os.path.exists(applied_netplan_yaml):
            os.remove(applied_netplan_yaml)
        for f in os.listdir(self.backup_dir):
            shutil.move(os.path.join(self.backup_dir, f), NETPLAN_DIR)
        os.removedirs(self.backup_dir)

        self._bring_down_networking()
        try:
            check_call(["netplan", "apply", "--debug"], timeout=60)
        except TimeoutExpired:
            sys.stderr.write("WARNING: netplan apply --debug timed out!\n")
            sys.stderr.flush()
        except CalledProcessError:
            sys.stderr.write("WARNING: netplan apply --debug failed!\n")
            sys.stderr.flush()
        if self.send_result:
            # Confirm we can still communicate with MAAS.
            signal_wrapper(
                self.url,
                self.creds,
                "APPLYING_NETCONF",
                "ephemeral netplan applied",
            )
        # The ephemeral environment network configuration only brings up the
        # PXE interface.
        get_interfaces(clear_cache=True)

    def __enter__(self):
        """Apply the user network configuration."""
        if not self.apply_configured_networking:
            return self
        output_and_send_scripts(
            "Applying custom network configuration for {msg_name}",
            self.scripts,
            self.send_result,
            status="APPLYING_NETCONF",
        )

        if not os.path.exists(self.netplan_yaml):
            # This should never happen, if it does it means the Metadata
            # server is sending us incomplete data.
            output_and_send_scripts(
                "Unable to apply custom network configuration for {msg_name}."
                "\n\nnetplan.yaml is missing from tar.",
                self.scripts,
                self.send_result,
                error_is_stderr=True,
                exit_status=1,
                status="APPLYING_NETCONF",
            )
            raise FileNotFoundError(self.netplan_yaml)

        # Backup existing netplan config
        os.makedirs(self.backup_dir, exist_ok=True)
        for f in os.listdir(NETPLAN_DIR):
            shutil.move(os.path.join(NETPLAN_DIR, f), self.backup_dir)
        # Place the customized netplan config in
        shutil.copy2(self.netplan_yaml, NETPLAN_DIR)

        self._bring_down_networking()
        # Apply the configuration.
        if not run_and_check(
            ["netplan", "apply", "--debug"],
            self.scripts,
            "APPLYING_NETCONF",
            self.send_result,
            True,
            lambda: self._apply_ephemeral_netplan(),
        ):
            raise OSError("netplan failed to apply!")

        # The new network configuration may change what devices are available.
        # Clear the cache and reload.
        get_interfaces(clear_cache=True)

        try:
            # Confirm we can still communicate with MAAS.
            if self.send_result:
                signal(
                    self.url,
                    self.creds,
                    "APPLYING_NETCONF",
                    "User netplan config applied",
                )
        except SignalException:
            self._apply_ephemeral_netplan()
            output_and_send_scripts(
                "Unable to communicate to the MAAS metadata service after "
                "applying custom network configuration.",
                self.scripts,
                self.send_result,
                error_is_stderr=True,
                exit_status=1,
                status="APPLYING_NETCONF",
            )
            raise
        else:
            # Network configuration successfully applied. Clear the logs so
            # only script output is reported
            _clean_logs(self.scripts)
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        # Only revert networking if custom networking was applied.
        if self.apply_configured_networking:
            self._apply_ephemeral_netplan()


# Cache the block devices so we only have to query once.
_block_devices = None
_block_devices_lock = Lock()


def get_block_devices():
    """If needed, query lsblk for all known block devices and store."""
    global _block_devices
    global _block_devices_lock
    # Grab lock if cache is blank and double check we really need to fill
    # cache once we get the lock.
    if _block_devices is None:
        _block_devices_lock.acquire()
    if _block_devices is None:
        try:
            cmd = [
                "lsblk",
                "--exclude",
                "1,2,7",
                "-d",
                "-P",
                "-o",
                "NAME,MODEL,SERIAL",
            ]
            block_list = check_output(cmd, timeout=60).decode("utf-8")
        except TimeoutExpired:
            _block_devices = KeyError(
                "%s timed out after 60 seconds" % " ".join(cmd)
            )
        except CalledProcessError as e:
            _block_devices = KeyError(
                "%s failed with return code %s\n\n"
                "STDOUT:\n%s\n\n"
                "STDERR:\n%s\n"
                % (" ".join(cmd), e.returncode, e.stdout, e.stderr)
            )
        else:
            block_devices = []
            for blockdev in block_list.splitlines():
                tokens = shlex.split(blockdev)
                current_block = {}
                for token in tokens:
                    k, v = token.split("=", 1)
                    current_block[k] = v.strip()
                block_devices.append(current_block)
            # LP:1732539 - Don't fill cache until all results are proceeded.
            _block_devices = block_devices

    if _block_devices_lock.locked():
        _block_devices_lock.release()

    if isinstance(_block_devices, KeyError):
        raise _block_devices
    else:
        return _block_devices


# Cache the interfaces so we only have to query once.
_interfaces = None
_interfaces_lock = Lock()


def get_interfaces(clear_cache=False):
    """If needed, read netplan configuration files to get interfaces list.

    Interfaces must be retrieved from the netplan configuration files to allow
    get_interfaces() to differentiate between configured and unconfigured
    interfaces. It also allows get_interfaces() to detect interfaces which
    are configured to change the interface name and/or MAC address."""
    global _interfaces
    global _interfaces_lock
    # Grab lock if cache is blank and double check we really need to fill
    # cache once we get the lock.
    if _interfaces is None or clear_cache:
        _interfaces_lock.acquire()
    if clear_cache:
        _interfaces = None
    if _interfaces is None:
        interfaces = {}
        for cfg_file in os.listdir(NETPLAN_DIR):
            cfg_path = os.path.join(NETPLAN_DIR, cfg_file)
            try:
                with open(cfg_path, "r") as f:
                    cfg = yaml.safe_load(f)
            except Exception:
                # Ignore bad configs, non-files.
                continue
            if not isinstance(cfg, dict):
                continue
            for value in cfg.get("network", {}).values():
                if not isinstance(value, dict):
                    continue
                for dev, info in value.items():
                    if "macaddress" in info:
                        macaddress = info["macaddress"]
                    elif "macaddress" in info.get("match", {}):
                        macaddress = info["match"]["macaddress"]
                    else:
                        continue
                    # We only care about devices which have addresses. This is
                    # how we differentiate between a bond and the interfaces
                    # that make that bond.
                    if (
                        "addresses" in info
                        or info.get("dhcp4")
                        or info.get("dhcp6")
                    ):
                        interfaces[macaddress] = info.get("set-name", dev)

        # XXX ltrager 2019-07-26 - netplan is non-blocking(LP:1838114). Try
        # to wait until all devices have come up. If this doesn't happen after
        # 5 seconds continue the error will be reported later.
        for naptime in (0.1, 0.1, 0.1, 0.2, 0.2, 0.3, 0.5, 0.5, 1, 2):
            live_devs = os.listdir("/sys/class/net")
            missing = False
            for dev in interfaces.values():
                if dev not in live_devs:
                    missing = True
                    break
            if missing:
                time.sleep(naptime)
            else:
                break
        if missing:
            sys.stderr.write("Error: Not all interfaces were brought up!")
        _interfaces = interfaces

    if _interfaces_lock.locked():
        _interfaces_lock.release()
    return _interfaces


def parse_parameters(script, scripts_dir):
    """Return a list containg script path and parameters to be passed to it."""
    ret = [os.path.join(scripts_dir, script["path"])]
    for param in script.get("parameters", {}).values():
        param_type = param.get("type")
        if param_type == "storage":
            value = param["value"]
            if value == "all":
                raise KeyError(
                    "MAAS did not detect any storage devices during "
                    "commissioning!"
                )
            model = value.get("model")
            serial = value.get("serial")
            if not (model and serial):
                # If no model or serial were included trust that id_path
                # is correct. This is needed for VirtIO devices.
                value["path"] = value["input"] = value["id_path"]
            else:
                # Map the current path of the device to what it currently is
                # for the device model and serial. This is needed as the
                # the device name may have changed since commissioning.
                for blockdev in get_block_devices():
                    if (
                        model == blockdev["MODEL"]
                        and serial == blockdev["SERIAL"]
                    ):
                        value["path"] = value["input"] = (
                            "/dev/%s" % blockdev["NAME"]
                        )
            argument_format = param.get("argument_format", "--storage={path}")
            try:
                ret += argument_format.format(**value).split()
            except KeyError:
                raise KeyError(
                    "Storage device '%s' with serial '%s' not found!\n\n"
                    "This indicates the storage device has been removed or "
                    "the OS is unable to find it due to a hardware failure. "
                    "Please re-commission this node to re-discover the "
                    "storage devices, or delete this device manually."
                    % (model, serial)
                )
        elif param_type == "interface":
            value = param["value"]
            if value == "all":
                raise KeyError(
                    "MAAS did not detect any interfaces during commissioning!"
                )
            try:
                value["input"] = value["name"] = get_interfaces()[
                    value["mac_address"]
                ]
                argument_format = param.get(
                    "argument_format", "--interface={name}"
                )
                ret += argument_format.format(**value).split()
            except KeyError:
                raise KeyError(
                    "Interface device %s (vendor: %s product: %s) with MAC "
                    "address %s has not been found!\n\n"
                    "This indicates the interface has been removed or the OS "
                    "is unable to find it due to a hardware failure. Please "
                    "re-commision this node to re-discover the interfaces or "
                    "delete this interface manually."
                    % (
                        value["name"],
                        value["vendor"],
                        value["product"],
                        value["mac_address"],
                    )
                )
        else:
            argument_format = param.get(
                "argument_format", "--%s={input}" % param_type
            )
            ret += argument_format.format(input=param["value"]).split()

    return ret


def _check_link_connected(script):
    # Only check if a link is connected if its a network script
    # which applies configured networking and has an interface parameter.
    if script["hardware_type"] != 4 or not script.get(
        "apply_configured_networking"
    ):
        return
    interface = None
    for param in script.get("parameters", {}).values():
        try:
            if param["type"] == "interface":
                interface = get_interfaces()[param["value"]["mac_address"]]
                break
        except KeyError:
            # The parameter's keys should be validated by parse_parameters
            # above. Ignore the missing value if something went wrong.
            return
    if not interface:
        return

    operstate_path = os.path.join("/sys/class/net", interface, "operstate")
    with open(operstate_path, "r") as f:
        link_connected = f.read().strip() == "up"

    # MAAS only allows testing an interface which is connected. If its still
    # connected there is nothing to report.
    if link_connected:
        return

    if os.path.exists(script["result_path"]):
        try:
            with open(script["result_path"], "r") as f:
                result = yaml.safe_load(f.read())
        except Exception:
            # Ignore errors reading the file so MAAS can report the error
            # to the user.
            return
        if not result or (isinstance(result, str) and not result.strip()):
            # Handle empty result files
            result = {}
        elif not isinstance(result, dict):
            # The script created an invalid result file. Ignore it so MAAS can
            # report the error to the user.
            return
        # Don't override link_connected if the script set it.
        if "link_connected" in result:
            return
        # If the test passed don't report link_connected status.
        if result.get("status") == "passed":
            return
        elif script["exit_status"] == 0:
            return
        os.remove(script["result_path"])
    else:
        # If the test passed don't report link_connected status.
        if script["exit_status"] == 0:
            return
        result = {}

    result["link_connected"] = link_connected
    with open(script["result_path"], "w") as f:
        yaml.safe_dump(result, f)


def run_script(script, scripts_dir, send_result=True):
    args = copy.deepcopy(script["args"])
    args["status"] = "WORKING"
    args["send_result"] = send_result
    timeout_seconds = script.get("timeout_seconds")
    for param in script.get("parameters", {}).values():
        if param.get("type") == "runtime":
            timeout_seconds = param["value"]
            break

    output_and_send("Starting %s" % script["msg_name"], **args)

    env = copy.deepcopy(os.environ)
    env["OUTPUT_COMBINED_PATH"] = script["combined_path"]
    env["OUTPUT_STDOUT_PATH"] = script["stdout_path"]
    env["OUTPUT_STDERR_PATH"] = script["stderr_path"]
    env["RESULT_PATH"] = script["result_path"]
    env["DOWNLOAD_PATH"] = script["download_path"]
    env["RUNTIME"] = str(timeout_seconds)
    env["HAS_STARTED"] = str(script.get("has_started", False))

    try:
        script_arguments = parse_parameters(script, scripts_dir)
    except KeyError as e:
        # 2 is the return code bash gives when it can't execute.
        script["exit_status"] = args["exit_status"] = 2
        output = "Unable to run '%s': %s\n\n" % (
            script["name"],
            str(e).replace('"', "").replace("\\n", "\n"),
        )
        output += "Given parameters:\n%s\n\n" % str(
            script.get("parameters", {})
        )
        try:
            output += "Discovered storage devices:\n%s\n" % str(
                get_block_devices()
            )
        except KeyError:
            pass
        output += "Discovered interfaces:\n%s\n" % str(get_interfaces())

        output = output.encode()
        args["files"] = {
            script["combined_name"]: output,
            script["stderr_name"]: output,
        }
        output_and_send(
            "Failed to execute %s: %d"
            % (script["msg_name"], args["exit_status"]),
            **args
        )
        return False

    try:
        # This script sets its own niceness value to the highest(-20) below
        # to help ensure the heartbeat keeps running. When launching the
        # script we need to lower the nice value as a child process
        # inherits the parent processes niceness value. preexec_fn is
        # executed in the child process before the command is run. When
        # setting the nice value the kernel adds the current nice value
        # to the provided value. Since the runner uses a nice value of -20
        # setting it to 40 gives the actual nice value of 20.
        proc = Popen(
            script_arguments,
            stdout=PIPE,
            stderr=PIPE,
            env=env,
            preexec_fn=lambda: os.nice(40),
        )
        capture_script_output(
            proc,
            script["combined_path"],
            script["stdout_path"],
            script["stderr_path"],
            timeout_seconds,
        )
    except OSError as e:
        if isinstance(e.errno, int) and e.errno != 0:
            script["exit_status"] = args["exit_status"] = e.errno
        else:
            # 2 is the return code bash gives when it can't execute.
            script["exit_status"] = args["exit_status"] = 2
        _check_link_connected(script)
        stderr = str(e).encode()
        if stderr == b"":
            stderr = b"Unable to execute script"
        args["files"] = {
            script["combined_name"]: stderr,
            script["stderr_name"]: stderr,
        }
        if os.path.exists(script["result_path"]):
            args["files"][script["result_name"]] = open(
                script["result_path"], "rb"
            ).read()
        output_and_send(
            "Failed to execute %s: %d"
            % (script["msg_name"], args["exit_status"]),
            **args
        )
        sys.stdout.write("%s\n" % stderr)
        sys.stdout.flush()
        return False
    except TimeoutExpired:
        # 124 is the exit status from the timeout command.
        script["exit_status"] = args["exit_status"] = 124
        args["status"] = "TIMEDOUT"
        _check_link_connected(script)
        args["files"] = {
            script["combined_name"]: open(
                script["combined_path"], "rb"
            ).read(),
            script["stdout_name"]: open(script["stdout_path"], "rb").read(),
            script["stderr_name"]: open(script["stderr_path"], "rb").read(),
        }
        if os.path.exists(script["result_path"]):
            args["files"][script["result_name"]] = open(
                script["result_path"], "rb"
            ).read()
        output_and_send(
            "Timeout(%s) expired on %s"
            % (str(timedelta(seconds=timeout_seconds)), script["msg_name"]),
            **args
        )
        return False
    else:
        script["exit_status"] = args["exit_status"] = proc.returncode
        _check_link_connected(script)
        args["files"] = {
            script["combined_name"]: open(
                script["combined_path"], "rb"
            ).read(),
            script["stdout_name"]: open(script["stdout_path"], "rb").read(),
            script["stderr_name"]: open(script["stderr_path"], "rb").read(),
        }
        if os.path.exists(script["result_path"]):
            args["files"][script["result_name"]] = open(
                script["result_path"], "rb"
            ).read()
        output_and_send(
            "Finished %s: %s" % (script["msg_name"], args["exit_status"]),
            **args
        )
        if proc.returncode != 0:
            return False
        else:
            return True


def run_serial_scripts(scripts, scripts_dir, config_dir, send_result=True):
    """Run scripts serially."""
    fail_count = 0
    for script in scripts:
        try:
            with CustomNetworking([script], config_dir, send_result):
                if not install_dependencies([script], send_result):
                    fail_count += 1
                    continue
                if not run_script(
                    script=script,
                    scripts_dir=scripts_dir,
                    send_result=send_result,
                ):
                    fail_count += 1
        except SignalException:
            fail_count += 1
        except Exception:
            traceback.print_exc()
            fail_count += 1
    return fail_count


def run_instance_scripts(scripts, scripts_dir, config_dir, send_result=True):
    """Run scripts in parallel with scripts with the same name."""
    fail_count = 0
    for script in scripts:
        instance_scripts = []
        for instance_script in scripts:
            # Don't run scripts which have already ran.
            if "thread" in instance_script:
                continue
            if instance_script["name"] == script["name"]:
                instance_scripts.append(instance_script)
        try:
            with CustomNetworking(instance_scripts, config_dir, send_result):
                if not install_dependencies(instance_scripts, send_result):
                    fail_count += len(instance_scripts)
                    continue
                for instance_script in instance_scripts:
                    instance_script["thread"] = Thread(
                        target=run_script,
                        name=script["msg_name"],
                        kwargs={
                            "script": instance_script,
                            "scripts_dir": scripts_dir,
                            "send_result": send_result,
                        },
                    )
                    instance_script["thread"].start()
                for instance_script in instance_scripts:
                    instance_script["thread"].join()
                    if instance_script.get("exit_status") != 0:
                        fail_count += 1
        except SignalException:
            fail_count += len(instance_scripts)
        except Exception:
            traceback.print_exc()
            fail_count += len(instance_scripts)
    return fail_count


def run_parallel_scripts(scripts, scripts_dir, config_dir, send_result=True):
    """Run scripts in parallel."""
    fail_count = 0
    # Make sure custom networking is only applied when the running script
    # requests it. Scripts which don't require custom networking(default)
    # run first.
    non_netconf_scripts = []
    netconf_scripts = []
    for script in scripts:
        if script.get("apply_configured_networking"):
            netconf_scripts.append(script)
        else:
            non_netconf_scripts.append(script)
    for nscripts in [non_netconf_scripts, netconf_scripts]:
        try:
            with CustomNetworking(nscripts, config_dir, send_result):
                # Start scripts which do not have dependencies first so they
                # can run while other scripts are installing.
                for script in sorted(
                    nscripts,
                    key=lambda i: (
                        len(i.get("packages", {}).keys()),
                        i["name"],
                    ),
                ):
                    if not install_dependencies([script], send_result):
                        fail_count += 1
                        continue
                    script["thread"] = Thread(
                        target=run_script,
                        name=script["msg_name"],
                        kwargs={
                            "script": script,
                            "scripts_dir": scripts_dir,
                            "send_result": send_result,
                        },
                    )
                    script["thread"].start()
                for script in nscripts:
                    script["thread"].join()
                    if script.get("exit_status") != 0:
                        fail_count += 1
        except SignalException:
            fail_count += len(nscripts)
        except Exception:
            traceback.print_exec()
            fail_count += len(nscripts)
    return fail_count


def run_scripts(url, creds, scripts_dir, out_dir, scripts, send_result=True):
    """Run and report results for the given scripts."""
    config_dir = os.path.abspath(os.path.join(scripts_dir, "..", "config"))
    serial_scripts = []
    instance_scripts = []
    parallel_scripts = []

    # Add extra info to the script dictionary used to run the script.
    # Run by CPU(1), memory(2), storage(3), network(4) and finally node(0).
    # Because node is hardware_type 0 use 99 when ordering so it always runs
    # last.
    for script in sorted(
        scripts,
        key=lambda i: (
            99 if i["hardware_type"] == 0 else i["hardware_type"],
            i["name"],
        ),
    ):
        # The arguments used to send MAAS data about the result of the script.
        script["args"] = {
            "url": url,
            "creds": creds,
            "script_result_id": script["script_result_id"],
        }
        # The pretty name of the script with id used for debug messages.
        script["msg_name"] = "%s (id: %s" % (
            script["name"],
            script["script_result_id"],
        )
        if "script_version_id" in script:
            script["msg_name"] = "%s, script_version_id: %s)" % (
                script["msg_name"],
                script["script_version_id"],
            )
            script["args"]["script_version_id"] = script["script_version_id"]
        else:
            script["msg_name"] = "%s)" % script["msg_name"]

        # Create a seperate output directory for each script being run as
        # multiple scripts with the same name may be run.
        script_out_dir = os.path.join(
            out_dir, "%s.%s" % (script["name"], script["script_result_id"])
        )
        os.makedirs(script_out_dir, exist_ok=True)
        script["combined_name"] = script["name"]
        script["combined_path"] = os.path.join(
            script_out_dir, script["combined_name"]
        )
        script["stdout_name"] = "%s.out" % script["name"]
        script["stdout_path"] = os.path.join(
            script_out_dir, script["stdout_name"]
        )
        script["stderr_name"] = "%s.err" % script["name"]
        script["stderr_path"] = os.path.join(
            script_out_dir, script["stderr_name"]
        )
        script["result_name"] = "%s.yaml" % script["name"]
        script["result_path"] = os.path.join(
            script_out_dir, script["result_name"]
        )
        script["download_path"] = os.path.join(
            scripts_dir, "downloads", script["name"]
        )
        # Make sure the download path always exists
        os.makedirs(script["download_path"], exist_ok=True)

        # The numeric values for hardware_type and parallel are defined in
        # enums in src/metadataserver/enum.py. When running on a node enum.py
        # is not available which is why they are hard coded here.
        if script["parallel"] == 0:
            serial_scripts.append(script)
        elif script["parallel"] == 1:
            instance_scripts.append(script)
        elif script["parallel"] == 2:
            parallel_scripts.append(script)

    fail_count = run_serial_scripts(
        serial_scripts, scripts_dir, config_dir, send_result
    )
    fail_count += run_instance_scripts(
        instance_scripts, scripts_dir, config_dir, send_result
    )
    fail_count += run_parallel_scripts(
        parallel_scripts, scripts_dir, config_dir, send_result
    )

    return fail_count


def run_scripts_from_metadata(
    url, creds, scripts_dir, out_dir, send_result=True, download=True
):
    """Run all scripts from a tar given by MAAS."""
    with open(os.path.join(scripts_dir, "index.json")) as f:
        scripts = json.load(f)["1.0"]

    fail_count = 0
    commissioning_scripts = scripts.get("commissioning_scripts")
    if commissioning_scripts is not None:
        sys.stdout.write("Starting commissioning scripts...\n")
        sys.stdout.flush()
        fail_count += run_scripts(
            url,
            creds,
            scripts_dir,
            out_dir,
            commissioning_scripts,
            send_result,
        )

    if fail_count != 0:
        output_and_send(
            "%d commissioning scripts failed to run" % fail_count,
            send_result,
            url,
            creds,
            "FAILED",
        )
        return fail_count

    # After commissioning has successfully finished redownload the scripts tar
    # in case new hardware was discovered that has an associated script with
    # the for_hardware field.
    if commissioning_scripts is not None and download:
        if not download_and_extract_tar(
            "%smaas-scripts" % url, creds, scripts_dir
        ):
            return fail_count
        return run_scripts_from_metadata(
            url, creds, scripts_dir, out_dir, send_result, download
        )

    testing_scripts = scripts.get("testing_scripts")
    if testing_scripts is not None:
        # If the node status was COMMISSIONING transition the node into TESTING
        # status. If the node is already in TESTING status this is ignored.
        if send_result:
            signal_wrapper(url, creds, "TESTING")

        sys.stdout.write("Starting testing scripts...\n")
        sys.stdout.flush()
        fail_count += run_scripts(
            url, creds, scripts_dir, out_dir, testing_scripts, send_result
        )

    return fail_count


class HeartBeat(Thread):
    """Creates a background thread which pings the MAAS metadata service every
    two minutes to let it know we're still up and running scripts. If MAAS
    doesn't hear from us it will assume something has gone wrong and power off
    the node.
    """

    def __init__(self, url, creds):
        super().__init__(name="HeartBeat")
        self._url = url
        self._creds = creds
        self._run = Event()
        self._run.set()

    def stop(self):
        self._run.clear()

    def run(self):
        # Record the relative start time of the entire run.
        start = time.monotonic()
        tenths = 0
        while self._run.is_set():
            # Record the start of this heartbeat interval.
            heartbeat_start = time.monotonic()
            heartbeat_elapsed = 0
            total_elapsed = heartbeat_start - start
            args = [self._url, self._creds, "WORKING"]
            # Log the elapsed time plus the measured clock skew, if this
            # is the second run through the loop.
            if tenths > 0:
                args.append(
                    "Elapsed time (real): %d.%ds; Python: %d.%ds"
                    % (
                        total_elapsed,
                        total_elapsed % 1 * 10,
                        tenths // 10,
                        tenths % 10,
                    )
                )
            signal_wrapper(*args)
            # Spin for 2 minutes before sending another heartbeat.
            while heartbeat_elapsed < 120 and self._run.is_set():
                heartbeat_end = time.monotonic()
                heartbeat_elapsed = heartbeat_end - heartbeat_start
                # Wake up every tenth of a second to record clock skew and
                # ensure delayed scheduling doesn't impact the heartbeat.
                time.sleep(0.1)
                tenths += 1


def main():
    parser = argparse.ArgumentParser(
        description="Download and run scripts from the MAAS metadata service."
    )
    parser.add_argument(
        "--config",
        metavar="file",
        help="Specify config file",
        default="/etc/cloud/cloud.cfg.d/91_kernel_cmdline_url.cfg",
    )
    parser.add_argument(
        "--ckey",
        metavar="key",
        help="The consumer key to auth with",
        default=None,
    )
    parser.add_argument(
        "--tkey",
        metavar="key",
        help="The token key to auth with",
        default=None,
    )
    parser.add_argument(
        "--csec",
        metavar="secret",
        help="The consumer secret (likely '')",
        default="",
    )
    parser.add_argument(
        "--tsec",
        metavar="secret",
        help="The token secret to auth with",
        default=None,
    )
    parser.add_argument(
        "--apiver",
        metavar="version",
        help='The apiver to use ("" can be used)',
        default=MD_VERSION,
    )
    parser.add_argument(
        "--url", metavar="url", help="The data source to query", default=None
    )
    parser.add_argument(
        "--no-send",
        action="store_true",
        default=False,
        help="Don't send results back to MAAS",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        default=False,
        help="Assume scripts have already been downloaded",
    )

    parser.add_argument(
        "storage_directory",
        nargs="?",
        default=os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
        help="Directory to store the extracted data from the metadata service.",
    )

    args = parser.parse_args()

    creds = {
        "consumer_key": args.ckey,
        "token_key": args.tkey,
        "token_secret": args.tsec,
        "consumer_secret": args.csec,
        "metadata_url": args.url,
    }

    if args.config:
        read_config(args.config, creds)

    url = creds.get("metadata_url")
    if url is None:
        fail("URL must be provided either in --url or in config\n")
    if url.endswith("/"):
        url = url[:-1]
    url = "%s/%s/" % (url, args.apiver)

    # Disable the OOM killer on the runner process, the OOM killer will still
    # go after any tests spawned.
    oom_score_adj_path = os.path.join(
        "/proc", str(os.getpid()), "oom_score_adj"
    )
    open(oom_score_adj_path, "w").write("-1000")
    # Give the runner the highest nice value to ensure the heartbeat keeps
    # running.
    os.nice(-20)

    # Make sure installing packages is noninteractive for this process
    # and all subprocesses.
    if "DEBIAN_FRONTEND" not in os.environ:
        os.environ["DEBIAN_FRONTEND"] = "noninteractive"

    heart_beat = HeartBeat(url, creds)
    if not args.no_send:
        heart_beat.start()

    scripts_dir = os.path.join(args.storage_directory, "scripts")
    os.makedirs(scripts_dir, exist_ok=True)
    out_dir = os.path.join(args.storage_directory, "out")
    os.makedirs(out_dir, exist_ok=True)

    has_content = True
    fail_count = 0
    if not args.no_download:
        has_content = download_and_extract_tar(
            "%smaas-scripts" % url, creds, scripts_dir
        )
    if has_content:
        fail_count = run_scripts_from_metadata(
            url,
            creds,
            scripts_dir,
            out_dir,
            not args.no_send,
            not args.no_download,
        )

    # Signal success or failure after all scripts have ran. This tells the
    # region to transistion the status.
    if fail_count == 0:
        output_and_send(
            "All scripts successfully ran", not args.no_send, url, creds, "OK"
        )
    else:
        output_and_send(
            "%d test scripts failed to run" % fail_count,
            not args.no_send,
            url,
            creds,
            "FAILED",
        )

    heart_beat.stop()


if __name__ == "__main__":
    main()
