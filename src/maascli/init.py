# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Methods related to initializing a MAAS deployment."""

import argparse
import json
import os
import subprocess
import sys
from textwrap import dedent

from maascli.configfile import MAASConfiguration
from macaroonbakery import httpbakery


def deprecated_for(new_option):
    """Return an arparse.Action for deprecating another option.

    It prints out a deprecation message and calls the new option.

    """

    class DeprecatedAction(argparse.Action):

        _new_option = new_option
        _deprecation_message = (
            'Note: "{option_string}" is deprecated and will be removed, '
            'please use "{new_option}" instead.'
        )

        def __init__(self, *args, **kwargs):
            kwargs.setdefault("help", argparse.SUPPRESS)
            kwargs["default"] = argparse.SUPPRESS
            super().__init__(*args, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            action = parser._option_string_actions.get(self._new_option)
            assert action, 'unknown option "{}"'.format(self._new_option)
            assert (
                option_string
            ), '"deprecate_for" must be used with optional arguments'
            print(
                self._deprecation_message.format(
                    option_string=option_string, new_option=self._new_option
                ),
                file=sys.stderr,
            )
            option_string = self._new_option
            return action(
                parser, namespace, values, option_string=option_string
            )

    return DeprecatedAction


def add_candid_options(parser):
    parser.add_argument(
        "--candid-agent-file",
        help="Agent file containing Candid authentication information",
    )
    parser.add_argument(
        "--candid-domain",
        default=None,
        help=(
            "The authentication domain to look up users in for the external "
            "Candid server."
        ),
    )
    parser.add_argument(
        "--candid-admin-group",
        default=None,
        help="Group of users whose members are made admins in MAAS",
    )


def add_rbac_options(parser):
    parser.add_argument(
        "--rbac-url",
        default=None,
        help="The URL for the Canonical RBAC service to use.",
    )
    parser.add_argument(
        "--rbac-service-name",
        default=None,
        help=(
            "Optionally, the name of the RBAC service to register this MAAS "
            "as.  If not provided, a list with services that the user can "
            "register will be displayed, to choose from."
        ),
    )


def add_create_admin_options(parser):
    parser.add_argument(
        "--admin-username",
        default=None,
        metavar="USERNAME",
        help="Username for the admin account.",
    )
    parser.add_argument(
        "--admin-password",
        default=None,
        metavar="PASSWORD",
        help="Force a given admin password instead of prompting.",
    )
    parser.add_argument(
        "--admin-email",
        default=None,
        metavar="EMAIL",
        help="Email address for the admin.",
    )
    parser.add_argument(
        "--admin-ssh-import",
        default=None,
        metavar="LP_GH_USERNAME",
        help=(
            "Import SSH keys from Launchpad (lp:user-id) or "
            "Github (gh:user-id) for the admin."
        ),
    )


def create_admin_account(options):
    """Create the first admin account."""
    print_create_header = not all(
        [options.admin_username, options.admin_password, options.admin_email]
    )
    if print_create_header:
        print_msg("Create first admin account")
    cmd = [get_maas_region_bin_path(), "createadmin"]
    if options.admin_username:
        cmd.extend(["--username", options.admin_username])
    if options.admin_password:
        cmd.extend(["--password", options.admin_password])
    if options.admin_email:
        cmd.extend(["--email", options.admin_email])
    if options.admin_ssh_import:
        cmd.extend(["--ssh-import", options.admin_ssh_import])
    subprocess.call(cmd)


def create_account_external_auth(auth_config, maas_config, bakery_client=None):
    """Make the user login via external auth to create the first admin."""
    print_msg("Please login with MAAS to ensure authentication is set up")
    if bakery_client is None:
        bakery_client = httpbakery.Client()

    maas_url = maas_config["maas_url"].strip("/")

    failed_msg = ""
    try:
        resp = bakery_client.request(
            "GET", "{}/accounts/discharge-request/".format(maas_url)
        )
        if resp.status_code != 200:
            failed_msg = "request failed with code {}".format(resp.status_code)
    except Exception as e:
        failed_msg = str(e)

    if failed_msg:
        print_msg(
            "An error occurred while waiting for the first user creation: "
            + failed_msg
        )
        return

    result = resp.json()
    username = result["username"]
    if auth_config["rbac_url"]:
        message = "Authentication is working."
        if result["is_superuser"]:
            message += " User '{}' is an Administrator".format(username)
    else:
        if result["is_superuser"]:
            message = "Administrator user '{}' created".format(username)
        else:
            admin_group = auth_config["external_auth_admin_group"]
            message = dedent(
                """\
                A user with username '{username}' has been created, but it's
                not a superuser. Please log in to MAAS with a user that
                belongs to the '{admin_group}' group to create an
                administrator user.
                """
            ).format(username=username, admin_group=admin_group)
    print_msg(message)


def configure_authentication(options):
    cmd = [get_maas_region_bin_path(), "configauth"]
    if options.rbac_url is not None:
        cmd.extend(["--rbac-url", options.rbac_url])
    if options.rbac_service_name is not None:
        cmd.extend(["--rbac-service-name", options.rbac_service_name])
    if options.candid_domain is not None:
        cmd.extend(["--candid-domain", options.candid_domain])
    if options.candid_agent_file is not None:
        cmd.extend(["--candid-agent-file", options.candid_agent_file])
    if options.candid_admin_group is not None:
        cmd.extend(["--candid-admin-group", options.candid_admin_group])
    subprocess.call(cmd)


def get_maas_region_bin_path():
    maas_region = "maas-region"
    if "SNAP" in os.environ:
        maas_region = os.path.join(os.environ["SNAP"], "bin", maas_region)
    return maas_region


def get_current_auth_config():
    cmd = [get_maas_region_bin_path(), "configauth", "--json"]
    output = subprocess.check_output(cmd)
    return json.loads(output)


def print_msg(msg="", newline=True):
    """Print a message to stdout.

    Flushes the message to ensure its written immediately.
    """
    print(msg, end=("\n" if newline else ""), flush=True)


def init_maas(options):
    if options.enable_candid:
        print_msg("Configuring authentication")
        configure_authentication(options)
    if not options.skip_admin:
        auth_config = get_current_auth_config()
        if auth_config["external_auth_url"]:
            maas_config = MAASConfiguration().get()
            create_account_external_auth(auth_config, maas_config)
        else:
            create_admin_account(options)


def read_input(prompt):
    """Reads input from stdin."""
    while True:
        try:
            data = input(prompt)
        except EOFError:
            # Ctrl-d was pressed?
            print()
            continue
        except KeyboardInterrupt:
            print()
            raise SystemExit(1)
        else:
            # The assumption is that, since Python 3 return a Unicode string
            # from input(), it has Done The Right Thing with respect to
            # character encoding.
            return data


def prompt_for_choices(prompt, choices, default=None, help_text=None):
    """Prompt requires specific choice answeres.

    If `help_text` is provided the 'help' is added as a choice.
    """
    invalid_msg = "Invalid input, try again"
    if help_text:
        invalid_msg += " or type 'help'"
    invalid_msg += "."
    value = None
    while True:
        value = read_input(prompt)
        if not value:
            if default:
                return default
            else:
                print_msg(invalid_msg)
                print_msg()
        elif value == "help" and help_text:
            print_msg(help_text)
            print_msg()
        elif value not in choices:
            print_msg(invalid_msg)
            print_msg()
        else:
            return value
