# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Django command: configure the authentication source."""

__all__ = []

import json

import attr
from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS
from maascli.init import (
    add_candid_options,
    add_rbac_options,
    prompt_for_choices,
    read_input,
)
from maasserver.macaroon_auth import APIError
from maasserver.models import Config
from maasserver.models.rbacsync import RBAC_ACTION, RBACLastSync, RBACSync
from maasserver.rbac import RBACUserClient
from maasserver.utils.dns import validate_url
from macaroonbakery.bakery import generate_key


@attr.s
class AuthDetails:

    url = attr.ib(default=None)
    domain = attr.ib(default="")
    user = attr.ib(default="")
    key = attr.ib(default="")
    admin_group = attr.ib(default="")
    rbac_url = attr.ib(default="")


class InvalidURLError(CommandError):
    """User did not provide a valid URL."""


def prompt_for_external_auth_url(existing_url):
    if existing_url == "":
        existing_url = "none"
    new_url = read_input(
        "URL to external Candid server [default={}]: ".format(existing_url)
    )
    if new_url == "":
        new_url = existing_url
    return new_url


def update_auth_details_from_agent_file(agent_file_name, auth_details):
    """Read a .agent file and return auth details."""
    try:
        with open(agent_file_name) as fh:
            details = json.load(fh)
    except (FileNotFoundError, PermissionError) as error:
        raise CommandError(str(error))
    try:
        agent_details = details.get("agents", []).pop(0)
    except IndexError:
        raise CommandError("No agent users found in agent file")
    # update the passed auth details
    auth_details.url = agent_details.get("url")
    auth_details.user = agent_details.get("username")
    auth_details.key = details.get("key", {}).get("private")


def update_auth_details_from_rbac_registration(auth_details, service_name):
    print("Please authenticate with the RBAC service to register this MAAS")
    client = RBACUserClient(auth_details.rbac_url)
    services = {
        service["name"]: service
        for service in client.get_registerable_services()
    }
    if service_name is None:
        if not services:
            raise CommandError(
                "No registerable MAAS service on the specified RBAC server"
            )
        service = _pick_service(services)
    else:
        service = services.get(service_name)
        if service is None:
            create_service = prompt_for_choices(
                "A service with the specified name was not found, "
                "do you want to create one? (yes/no) [default=no]? ",
                ["yes", "no"],
                default="no",
            )
            if create_service == "no":
                raise CommandError("Registration with RBAC service canceled")
            try:
                service = client.create_service(service_name)
            except APIError as error:
                if error.status_code == 409:
                    raise CommandError(
                        "User not allowed to register this service"
                    )
                raise CommandError(str(error))
    _register_service(client, service, auth_details)
    print('Service "{}" registered'.format(service["name"]))


def _register_service(client, service, auth_details):
    key = generate_key()
    response = client.register_service(service["$uri"], str(key.public_key))
    auth_details.url = response["url"]
    auth_details.user = response["username"]
    auth_details.key = str(key)


def _pick_service(services):
    print("Please select which service to register this MAAS as:\n")
    services_list = sorted(
        (service["name"], service["pending"]) for service in services.values()
    )
    for idx, (name, pending) in enumerate(services_list, 1):
        print(
            " {:3} - {} {}".format(idx, name, "(pending)" if pending else "")
        )
    print()
    idx = read_input("Select service index: ")
    try:
        service_name = services_list[int(idx) - 1][0]
    except (ValueError, IndexError):
        raise CommandError("Invalid index")
    return services[service_name]


def is_valid_url(auth_url):
    try:
        validate_url(auth_url)
    except ValidationError:
        return False
    return True


def get_auth_config(config_manager):
    config_keys = [
        "external_auth_url",
        "external_auth_domain",
        "external_auth_user",
        "external_auth_key",
        "external_auth_admin_group",
        "rbac_url",
    ]
    return {key: config_manager.get_config(key) for key in config_keys}


def set_auth_config(config_manager, auth_details):
    config_manager.set_config("external_auth_url", auth_details.url or "")
    config_manager.set_config("external_auth_domain", auth_details.domain)
    config_manager.set_config("external_auth_user", auth_details.user)
    config_manager.set_config("external_auth_key", auth_details.key)
    config_manager.set_config(
        "external_auth_admin_group", auth_details.admin_group
    )
    config_manager.set_config("rbac_url", auth_details.rbac_url)

    # Clear the last sync, so if a new sync needs to occur it will do a full
    # sync with the RBAC service.
    RBACLastSync.objects.all().delete()
    if not auth_details.rbac_url:
        # No RBAC so remove all the sync trigger information, not needed
        # because syncing will no longer occur.
        RBACSync.objects.all().delete()
    else:
        # Force a full sync with the RBAC service. This is needed when the
        # rbac_url is the same as its previous value (but is actually) a
        # different RBAC service.
        RBACSync.objects.create(
            action=RBAC_ACTION.FULL,
            resource_type="",
            resource_name="",
            source="configauth command called",
        )


def clear_user_sessions():
    Session.objects.all().delete()


class Command(BaseCommand):
    help = "Configure external authentication."

    def add_arguments(self, parser):
        add_candid_options(parser)
        add_rbac_options(parser)
        parser.add_argument(
            "--json",
            action="store_true",
            default=False,
            help="Return the current authentication configuration as JSON",
        )

    def handle(self, *args, **options):
        config_manager = Config.objects.db_manager(DEFAULT_DB_ALIAS)

        if options.get("json"):
            print(json.dumps(get_auth_config(config_manager)))
            return

        auth_details = AuthDetails()

        auth_details.rbac_url = _get_or_prompt(
            options,
            "rbac_url",
            "URL for the Canonical RBAC service "
            "(leave blank if not using the service): ",
            replace_none=True,
        )
        if auth_details.rbac_url:
            if not is_valid_url(auth_details.rbac_url):
                raise InvalidURLError(
                    "Please enter a valid http or https URL."
                )
            update_auth_details_from_rbac_registration(
                auth_details, options.get("rbac_service_name")
            )
        else:
            agent_file = _get_or_prompt(
                options,
                "candid_agent_file",
                "Path of the Candid authentication agent file (leave "
                "blank if not using the service): ",
                replace_none=True,
            )
            if auth_details.rbac_url and not agent_file:
                raise CommandError(
                    "Candid authentication must be set when using RBAC"
                )
            if agent_file:
                update_auth_details_from_agent_file(agent_file, auth_details)
                if not auth_details.rbac_url:
                    auth_details.domain = _get_or_prompt(
                        options,
                        "candid_domain",
                        "Users domain for external authentication backend "
                        "(leave blank for empty): ",
                        replace_none=True,
                    )
                    auth_details.admin_group = _get_or_prompt(
                        options,
                        "candid_admin_group",
                        "Group of users whose members are made admins in MAAS "
                        "(leave blank for empty): ",
                    )

        set_auth_config(config_manager, auth_details)
        clear_user_sessions()


def _get_or_prompt(options, option, message, replace_none=False):
    """Return a config option either from command line or interactive input."""
    config = options.get(option)
    if config is None:
        config = read_input(message)
    if replace_none and config == "none":
        config = ""
    if config is None:
        config = ""
    return config
