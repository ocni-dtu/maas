# Copyright 2012-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""CLI management commands."""

__all__ = ["register_cli_commands"]

from functools import partial
import os
import pkgutil
import sys
from textwrap import fill

from apiclient.creds import convert_tuple_to_string
from maascli.api import fetch_api_description
from maascli.auth import (
    check_valid_apikey,
    obtain_credentials,
    UnexpectedResponse,
)
from maascli.command import Command
from maascli.config import ProfileConfig
from maascli.init import (
    add_candid_options,
    add_create_admin_options,
    add_rbac_options,
    init_maas,
)
from maascli.utils import api_url, parse_docstring, safe_name


class cmd_login(Command):
    """Log in to a remote API, and remember its description and credentials.

    If credentials are not provided on the command-line, they will be prompted
    for interactively.
    """

    def __init__(self, parser):
        super(cmd_login, self).__init__(parser)
        parser.add_argument(
            "profile_name",
            metavar="profile-name",
            help=(
                "The name with which you will later refer to this remote "
                "server and credentials within this tool."
            ),
        )
        parser.add_argument(
            "url",
            type=api_url,
            help=(
                "The URL of the remote API, e.g. http://example.com/MAAS/ "
                "or http://example.com/MAAS/api/2.0/ if you wish to specify "
                "the API version."
            ),
        )
        parser.add_argument(
            "credentials",
            nargs="?",
            default=None,
            help=(
                "The credentials, also known as the API key, for the "
                "remote MAAS server. These can be found in the user "
                "preferences page in the web UI; they take the form of "
                "a long random-looking string composed of three parts, "
                "separated by colons."
            ),
        )
        parser.add_argument(
            "-k",
            "--insecure",
            action="store_true",
            help="Disable SSL certificate check",
            default=False,
        )
        parser.set_defaults(credentials=None)

    def __call__(self, options):
        # Try and obtain credentials interactively if they're not given, or
        # read them from stdin if they're specified as "-".
        credentials = obtain_credentials(options.url, options.credentials)
        # Check for bogus credentials. Do this early so that the user is not
        # surprised when next invoking the MAAS CLI.
        if credentials is not None:
            try:
                valid_apikey = check_valid_apikey(
                    options.url, credentials, options.insecure
                )
            except UnexpectedResponse as e:
                raise SystemExit("%s" % e)
            else:
                if not valid_apikey:
                    raise SystemExit("The MAAS server rejected your API key.")
        # Get description of remote API.
        description = fetch_api_description(options.url, options.insecure)
        # Save the config.
        profile_name = options.profile_name
        with ProfileConfig.open(create=True) as config:
            config[profile_name] = {
                "credentials": credentials,
                "description": description,
                "name": profile_name,
                "url": options.url,
            }
            profile = config[profile_name]
        self.print_whats_next(profile)

    @staticmethod
    def print_whats_next(profile):
        """Explain what to do next."""
        what_next = [
            "You are now logged in to the MAAS server at {url} "
            "with the profile name '{name}'.",
            "For help with the available commands, try:",
            "  maas {name} --help",
        ]
        print()
        for message in what_next:
            message = message.format(**profile)
            print(fill(message))
            print()


class cmd_refresh(Command):
    """Refresh the API descriptions of all profiles.

    This retrieves the latest version of the help information for each
    profile.  Use it to update your command-line client's information after
    an upgrade to the MAAS server.
    """

    def __call__(self, options):
        try:
            with ProfileConfig.open() as config:
                for profile_name in config:
                    profile = config[profile_name]
                    url = profile["url"]
                    profile["description"] = fetch_api_description(url)
                    config[profile_name] = profile
        except FileNotFoundError:
            return


class cmd_logout(Command):
    """Log out of a remote API, purging any stored credentials.

    This will remove the given profile from your command-line  client.  You
    can re-create it by logging in again later.
    """

    def __init__(self, parser):
        super(cmd_logout, self).__init__(parser)
        parser.add_argument(
            "profile_name",
            metavar="profile-name",
            help=(
                "The name with which a remote server and its credentials "
                "are referred to within this tool."
            ),
        )

    def __call__(self, options):
        with ProfileConfig.open() as config:
            del config[options.profile_name]


class cmd_list(Command):
    """List remote APIs that have been logged-in to."""

    def __call__(self, options):
        try:
            with ProfileConfig.open() as config:
                for profile_name in config:
                    profile = config[profile_name]
                    url = profile["url"]
                    creds = profile["credentials"]
                    if creds is None:
                        print(profile_name, url)
                    else:
                        creds = convert_tuple_to_string(creds)
                        print(profile_name, url, creds)
        except FileNotFoundError:
            return


class cmd_init(Command):
    """Initialize controller."""

    def __init__(self, parser):
        super().__init__(parser)
        parser.add_argument(
            "--skip-admin",
            action="store_true",
            help="Skip the admin creation.",
        )
        add_create_admin_options(parser)
        parser.add_argument(
            "--enable-candid",
            default=False,
            action="store_true",
            help=(
                "Enable configuring the use of an external Candid server. "
                "If this isn't enabled, all --candid-* arguments "
                "will be ignored."
            ),
        )
        add_candid_options(parser)
        add_rbac_options(parser)

    def __call__(self, options):
        init_maas(options)


# Built-in commands to the maascli.
commands = {
    "login": cmd_login,
    "logout": cmd_logout,
    "list": cmd_list,
    "refresh": cmd_refresh,
}

# Commands to expose in the maascli when installed on a machine with
# python3-maasserver.
regiond_commands = (
    ("apikey", "maasserver", None),
    ("configauth", "maasserver", None),
    ("createadmin", "maasserver", None),
    (
        "changepassword",
        "django.contrib.auth",
        "Change a MAAS user's password.",
    ),
)


def register_cli_commands(parser):
    """Register the CLI's meta-subcommands on `parser`."""
    for name, command in commands.items():
        help_title, help_body = parse_docstring(command)
        command_parser = parser.subparsers.add_parser(
            safe_name(name),
            help=help_title,
            description=help_title,
            epilog=help_body,
        )
        command_parser.set_defaults(execute=command(command_parser))

    # Setup the snap commands into the maascli if in a snap and command exists.
    if "SNAP" in os.environ:
        # Only import snappy if running under the snap.
        from maascli import snappy

        extra_commands = [
            ("init", snappy.cmd_init),
            ("config", snappy.cmd_config),
            ("status", snappy.cmd_status),
            ("migrate", snappy.cmd_migrate),
        ]
    elif is_maasserver_available():
        extra_commands = [("init", cmd_init)]
    else:
        extra_commands = []

    for name, command in extra_commands:
        help_title, help_body = parse_docstring(command)
        command_parser = parser.subparsers.add_parser(
            safe_name(name),
            help=help_title,
            description=help_title,
            epilog=help_body,
        )
        command_parser.set_defaults(execute=command(command_parser))

    # Setup and the allowed django commands into the maascli.
    management = get_django_management()
    if management is not None and is_maasserver_available():
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE", "maasserver.djangosettings.settings"
        )
        from django import setup as django_setup

        django_setup()
        load_regiond_commands(management, parser)


def get_django_management():
    """Load the Django management module."""
    try:
        from django.core import management
    except ImportError:
        # No django installed so nothing to do.
        return None
    else:
        return management


def is_maasserver_available():
    """Ensure that 'maasserver' module is available."""
    return pkgutil.find_loader("maasserver") is not None


def run_regiond_command(management, parser):
    """Called to run the regiond command.

    The command itself is sys.argv[1] so that is not passed into this function.
    """
    # At present, only root should execute regiond commands
    if os.getuid() != 0:
        raise SystemExit("You can only '%s' as root." % sys.argv[1])
    management.execute_from_command_line()


def load_regiond_commands(management, parser):
    """Load the allowed regiond commands into the MAAS cli."""
    for name, app, help_text in regiond_commands:
        klass = management.load_command_class(app, name)
        if help_text is None:
            help_text = klass.help
        command_parser = parser.subparsers.add_parser(
            safe_name(name), help=help_text, description=help_text
        )
        klass.add_arguments(command_parser)
        command_parser.set_defaults(
            execute=partial(run_regiond_command, management)
        )
