# Copyright 2012-2018 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""
Driver parameters.

Each possible value of a Node's power_type field can be associated with
specific 'parameters' which will be used when controlling the node in
question.  These 'parameters' will be stored as a JSON object in the
Node's power parameter field and its related BMC.  Even if we want
to allow arbitrary parameters to be set using the API for maximum
flexibility, each value of power or pod type is associated with a set of
'sensible' parameters.  That is used to validate data (but again, it is
possible to bypass that validation step and store arbitrary parameters) and by
the UI to display the right parameter fields that correspond to the
selected power or pod type.  The classes in this module are used to
associate each power or pod type with a set of parameters.

The power and pod types are retrieved from the PowerDriverRegistry using
the json schema provisioningserver.drivers.power.JSON_POWER_DRIVERS_SCHEMA and
provisioningserver.drivers.pod.JSON_POD_DRIVERS_SCHEMA respectively.
To add new parameters requires changes to drivers that run in the rack
controllers.
"""

__all__ = [
    "get_all_power_types",
    "get_driver_choices",
    "get_driver_parameters",
]

from copy import deepcopy
from operator import itemgetter

from django import forms
from jsonschema import validate
from maasserver.clusterrpc.utils import call_clusters
from maasserver.config_forms import DictCharField
from maasserver.fields import MACAddressFormField
from maasserver.utils.forms import compose_invalid_choice_text
from provisioningserver.drivers import SETTING_PARAMETER_FIELD_SCHEMA
from provisioningserver.drivers.nos import JSON_NOS_DRIVERS_SCHEMA
from provisioningserver.drivers.power import JSON_POWER_DRIVERS_SCHEMA
from provisioningserver.drivers.power.registry import PowerDriverRegistry
from provisioningserver.rpc import cluster


FIELD_TYPE_MAPPINGS = {
    "string": forms.CharField,
    "mac_address": MACAddressFormField,
    "choice": forms.ChoiceField,
    # This is used on the API so a password field is just a char field.
    "password": forms.CharField,
}


def make_form_field(json_field):
    """Build a Django form field based on the JSON spec.

    :param json_field: The JSON-specified field to convert into a valid
        Djangoism.
    :type json_field: dict
    :return: The correct Django form field for the field type, as
        specified in FIELD_TYPE_MAPPINGS.
    """
    field_class = FIELD_TYPE_MAPPINGS.get(
        json_field["field_type"], forms.CharField
    )
    if json_field["field_type"] == "choice":
        invalid_choice_message = compose_invalid_choice_text(
            json_field["name"], json_field["choices"]
        )
        extra_parameters = {
            "choices": json_field["choices"],
            "error_messages": {"invalid_choice": invalid_choice_message},
        }
    else:
        extra_parameters = {}

    default = json_field.get("default")
    if default is not None:
        extra_parameters["initial"] = default

    form_field = field_class(
        label=json_field["label"],
        required=json_field["required"],
        **extra_parameters
    )
    return form_field


def add_power_driver_parameters(
    driver_type,
    name,
    description,
    fields,
    missing_packages,
    parameters_set,
    queryable=None,
):
    """Add new power type parameters to the given parameters_set if it
    does not already exist.

    :param driver_type: Type of driver. Either `power` or `pod`.
    :type driver_type: string
    :param name: The name of the power type for which to add parameters.
    :type name: string
    :param description: A longer description of the power type. This
        will be displayed in the UI.
    :type description: string
    :param fields: The fields that make up the parameters for the power
        type. Will be validated against
        SETTING_PARAMETER_FIELD_SCHEMA.
    :param missing_packages: System packages that must be installed on
        the cluster before the power type can be used.
    :type fields: list of `make_setting_field` results.
    :param parameters_set: An existing list of power type parameters to
        mutate.
    :type parameters_set: list
    """
    for power_type in parameters_set:
        if name == power_type["name"]:
            return
    field_set_schema = {
        "title": "Power type parameters field set schema",
        "type": "array",
        "items": SETTING_PARAMETER_FIELD_SCHEMA,
    }
    validate(fields, field_set_schema)
    params = {
        "driver_type": driver_type,
        "name": name,
        "description": description,
        "fields": fields,
        "missing_packages": missing_packages,
    }
    if queryable is not None:
        params["queryable"] = queryable
    parameters_set.append(params)


def get_driver_parameters_from_json(
    json_power_type_parameters,
    initial_power_params=None,
    skip_check=False,
    scope=None,
):
    """Return power type parameters.

    :param json_power_type_parameters: Power type parameters expressed
        as a JSON string or as set of JSONSchema-verifiable objects.
        Will be validated using jsonschema.validate().
    :type json_power_type_parameters: JSON string or iterable.
    :param initial_power_params: Power paramaters that were already set, any
        field which matches will have its initial value set.
    :type initial_power_params: dict
    :param skip_check: Whether the field should be checked or not.
    :type skip_check: bool
    :return: A dict of power parameters for all power types, indexed by
        power type name.
    """
    validate(json_power_type_parameters, JSON_POWER_DRIVERS_SCHEMA)
    power_parameters = {
        # Empty type, for the case where nothing is entered in the form yet.
        "": DictCharField([], required=False, skip_check=True)
    }
    if initial_power_params is None:
        initial_power_params = []
    for power_type in json_power_type_parameters:
        fields = []
        has_required_field = False
        for json_field in power_type["fields"]:
            # Skip fields that do not match the scope.
            if scope is not None and json_field["scope"] != scope:
                continue
            field_name = json_field["name"]
            if field_name in initial_power_params:
                json_field["default"] = initial_power_params[field_name]
            has_required_field = has_required_field or json_field["required"]
            fields.append((json_field["name"], make_form_field(json_field)))
        params = DictCharField(
            fields, required=has_required_field, skip_check=skip_check
        )
        power_parameters[power_type["name"]] = params
    return power_parameters


def get_driver_parameters(initial_power_params=None, skip_check=False):
    params = get_all_power_types()
    return get_driver_parameters_from_json(
        params, initial_power_params, skip_check
    )


def get_driver_choices():
    """Mutate the power types returned from the cluster into a choices
    structure as used by Django.

    :return: list of (name, description) tuples
    """
    return [
        (name, description)
        for name, description in get_driver_types(ignore_errors=True).items()
    ]


def get_driver_types(controllers=None, ignore_errors=False):
    """Return the choice of mechanism to control a node's power.

    :param controllers: Restrict to power types on the supplied
        :class:`RackController`s.
    :param ignore_errors: If comms errors are encountered talking to any
        clusters, ignore and carry on. This means partial data may be
        returned if other clusters are operational.

    :raises: :class:`ClusterUnavailable` if ignore_errors is False and a
        cluster controller is unavailable.

    :return: Dictionary mapping power type to its description.
    """
    types = dict()
    params = get_all_power_types(
        controllers=controllers, ignore_errors=ignore_errors
    )
    for power_type in params:
        types[power_type["name"]] = power_type["description"]
    return types


def get_all_power_types(controllers=None, ignore_errors=True):
    """Query the PowerDriverRegistry and obtain all known power driver types.

    :return: a list of power types matching the schema
        provisioningserver.drivers.power.JSON_POWER_DRIVERS_SCHEMA or
        provisioningserver.drivers.pod.JSON_POD_DRIVERS_SCHEMA
    """
    merged_types = []
    for power_type_orig in PowerDriverRegistry.get_schema(
        detect_missing_packages=False
    ):
        power_type = deepcopy(power_type_orig)
        driver_type = power_type.get("driver_type", "power")
        name = power_type["name"]
        fields = power_type.get("fields", [])
        description = power_type["description"]
        missing_packages = power_type["missing_packages"]
        queryable = power_type.get("queryable")
        add_power_driver_parameters(
            driver_type,
            name,
            description,
            fields,
            missing_packages,
            merged_types,
            queryable=queryable,
        )
    return sorted(merged_types, key=itemgetter("description"))


def add_nos_driver_parameters(
    driver_type, name, description, fields, parameters_set, deployable=None
):
    """
    Add new NOS type parameters to the given parameters_set if it
    does not already exist.

    :param driver_type: Type of driver. Must be 'nos'.
    :type driver_type: string
    :param name: The name of the NOS type for which to add parameters.
    :type name: string
    :param description: A longer description of the NOS type. This
        will be displayed in the UI.
    :type description: string
    :param fields: The fields that make up the parameters for the NOS type.
        Will be validated against SETTING_PARAMETER_FIELD_SCHEMA.
    :type fields: list of `make_setting_field` results.
    :param parameters_set: An existing list of NOS type parameters to
        mutate.
    :type parameters_set: list
    """
    for power_type in parameters_set:
        if name == power_type["name"]:
            return
    field_set_schema = {
        "title": "NOS type parameters field set schema",
        "type": "array",
        "items": SETTING_PARAMETER_FIELD_SCHEMA,
    }
    validate(fields, field_set_schema)
    assert driver_type == "nos", "NOS driver type must be 'nos'."
    params = {
        "driver_type": driver_type,
        "name": name,
        "description": description,
        "fields": fields,
    }
    if deployable is not None:
        params["deployable"] = deployable
    parameters_set.append(params)


def get_all_nos_types_from_racks(controllers=None, ignore_errors=True):
    """Query every rack controller and obtain all known NOS driver types.

    :return: a list of power types matching the schema
        provisioningserver.drivers.nos.JSON_NOS_DRIVERS_SCHEMA.
    """
    merged_types = []
    responses = call_clusters(
        cluster.DescribeNOSTypes,
        controllers=controllers,
        ignore_errors=ignore_errors,
    )
    for response in responses:
        nos_types = response["nos_types"]
        for nos_type in nos_types:
            driver_type = nos_type.get("driver_type", "nos")
            name = nos_type["name"]
            fields = nos_type.get("fields", [])
            description = nos_type["description"]
            deployable = nos_type.get("deployable")
            add_nos_driver_parameters(
                driver_type,
                name,
                description,
                fields,
                merged_types,
                deployable=deployable,
            )
    validate(merged_types, JSON_NOS_DRIVERS_SCHEMA)
    return sorted(merged_types, key=itemgetter("description"))
