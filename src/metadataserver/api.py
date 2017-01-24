# Copyright 2012-2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Metadata API."""

__all__ = [
    'AnonMetaDataHandler',
    'CommissioningScriptsHandler',
    'CurtinUserDataHandler',
    'IndexHandler',
    'MetaDataHandler',
    'UserDataHandler',
    'VersionIndexHandler',
    ]

import base64
import bz2
from functools import partial
import http.client
from io import BytesIO
from itertools import chain
import json
import tarfile
import time

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from formencode.validators import (
    Int,
    String,
)
from maasserver.api.nodes import store_node_power_parameters
from maasserver.api.support import (
    operation,
    OperationsHandler,
)
from maasserver.api.utils import (
    extract_oauth_key,
    get_mandatory_param,
    get_optional_param,
)
from maasserver.enum import (
    NODE_STATUS,
    NODE_STATUS_CHOICES_DICT,
    NODE_TYPE,
)
from maasserver.exceptions import (
    MAASAPIBadRequest,
    MAASAPINotFound,
    NodeStateViolation,
)
from maasserver.models import (
    Interface,
    Node,
    SSHKey,
    SSLKey,
)
from maasserver.models.event import Event
from maasserver.models.tag import Tag
from maasserver.populate_tags import populate_tags_for_single_node
from maasserver.preseed import (
    get_curtin_userdata,
    get_enlist_preseed,
    get_enlist_userdata,
    get_preseed,
)
from maasserver.utils import find_rack_controller
from maasserver.utils.orm import get_one
from metadataserver import logger
from metadataserver.enum import (
    SCRIPT_STATUS,
    SCRIPT_TYPE,
    SIGNAL_STATUS,
)
from metadataserver.models import (
    NodeKey,
    NodeUserData,
    Script,
    ScriptResult,
)
from metadataserver.models.commissioningscript import (
    add_script_to_archive,
    NODE_INFO_SCRIPTS,
)
from metadataserver.user_data import poweroff
from metadataserver.vendor_data import get_vendor_data
from piston3.utils import rc
from provisioningserver.events import (
    EVENT_DETAILS,
    EVENT_TYPES,
)
import yaml


class UnknownMetadataVersion(MAASAPINotFound):
    """Not a known metadata version."""


class UnknownNode(MAASAPINotFound):
    """Not a known node."""


def get_node_for_request(request):
    """Return the `Node` that `request` queries metadata for.

    For this form of access, a node can only query its own metadata.  Thus
    the oauth key used to authenticate the request must belong to the same
    node that is being queried.  Any request that is not made by an
    authenticated node will be denied.
    """
    key = extract_oauth_key(request)
    try:
        return NodeKey.objects.get_node_for_key(key)
    except NodeKey.DoesNotExist:
        raise PermissionDenied("Not authenticated as a known node.")


def get_node_for_mac(mac):
    """Identify node being queried based on its MAC address.

    This form of access is a security hazard, and thus it is permitted only
    on development systems where ALLOW_UNSAFE_METADATA_ACCESS is enabled.
    """
    if not settings.ALLOW_UNSAFE_METADATA_ACCESS:
        raise PermissionDenied(
            "Unauthenticated metadata access is not allowed on this MAAS.")
    match = get_one(Interface.objects.filter(mac_address=mac))
    if match is None:
        raise MAASAPINotFound()
    return match.node


def get_queried_node(request, for_mac=None):
    """Identify and authorize the node whose metadata is being queried.

    :param request: HTTP request.  In normal usage, this is authenticated
        with an oauth key; the key maps to the querying node, and the
        querying node always queries itself.
    :param for_mac: Optional MAC address for the node being queried.  If
        this is given, and anonymous metadata access is enabled (do in
        development environments only!) then the node is looked up by its
        MAC address.
    :return: The :class:`Node` whose metadata is being queried.
    """
    if for_mac is None:
        # Identify node, and authorize access, by oauth key.
        return get_node_for_request(request)
    else:
        # Access keyed by MAC address.
        return get_node_for_mac(for_mac)


def make_text_response(contents):
    """Create a response containing `contents` as plain text."""
    # XXX: Set a charset for text/plain. Django automatically encodes
    # non-binary content using DEFAULT_CHARSET (which is UTF-8 by default) but
    # only sets the charset parameter in the content-type header when a
    # content-type is NOT provided.
    return HttpResponse(contents, content_type='text/plain')


def make_list_response(items):
    """Create an `HttpResponse` listing `items`, one per line."""
    return make_text_response('\n'.join(items))


def check_version(version):
    """Check that `version` is a supported metadata version."""
    if version not in ('latest', '2012-03-01'):
        raise UnknownMetadataVersion("Unknown metadata version: %s" % version)


def add_event_to_node_event_log(
        node, origin, action, description, result=None):
    """Add an entry to the node's event log."""
    if node.status == NODE_STATUS.COMMISSIONING:
        if result in ['SUCCESS', None]:
            type_name = EVENT_TYPES.NODE_COMMISSIONING_EVENT
        else:
            type_name = EVENT_TYPES.NODE_COMMISSIONING_EVENT_FAILED
    elif node.status == NODE_STATUS.DEPLOYING:
        if result in ['SUCCESS', None]:
            type_name = EVENT_TYPES.NODE_INSTALL_EVENT
        else:
            type_name = EVENT_TYPES.NODE_INSTALL_EVENT_FAILED
    elif node.status == NODE_STATUS.ENTERING_RESCUE_MODE:
        if result in ['SUCCESS', None]:
            type_name = EVENT_TYPES.NODE_ENTERING_RESCUE_MODE_EVENT
        else:
            type_name = EVENT_TYPES.NODE_ENTERING_RESCUE_MODE_EVENT_FAILED
    elif node.node_type in [
            NODE_TYPE.RACK_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER]:
        type_name = EVENT_TYPES.REQUEST_CONTROLLER_REFRESH
    else:
        type_name = EVENT_TYPES.NODE_STATUS_EVENT

    event_details = EVENT_DETAILS[type_name]
    return Event.objects.register_event_and_event_type(
        node.system_id, type_name, type_level=event_details.level,
        type_description=event_details.description,
        event_action=action,
        event_description="'%s' %s" % (origin, description))


def process_file(
        results, script_set, script_name, content, request):
    """Process a file sent to MAAS over the metadata service."""

    script_result_id = get_optional_param(
        request, 'script_result_id', None, Int)

    # The .err indicates this should be stored in the STDERR column of the
    # ScriptResult. When finding or creating a ScriptResult don't include the
    # .err in the name. If given, we look up by script_result_id along with
    # the name to allow .err in the name.
    if script_name.endswith('.err'):
        script_name = script_name[0:-4]
        key = 'stderr'
    else:
        key = 'stdout'

    try:
        script_result = script_set.scriptresult_set.get(id=script_result_id)
    except ScriptResult.DoesNotExist:
        # If the script_result_id doesn't exist or wasn't sent try to find the
        # ScriptResult by script_name. Since ScriptResults can get their name
        # from the Script they are linked to or its own script_name field we
        # have to iterate over the list of script_results.
        script_result_found = False
        for script_result in script_set:
            if script_result.name == script_name:
                script_result_found = True
                break

        # If the ScriptResult wasn't found by id or name create an entry for
        # it.
        if not script_result_found:
            script_result = ScriptResult.objects.create(
                script_set=script_set, script_name=script_name,
                status=SCRIPT_STATUS.RUNNING)

    # Store the processed file in the given results dictionary. This allows
    # requests with multipart file uploads to include STDOUT and STDERR.
    if script_result in results:
        results[script_result][key] = content
    else:
        # Internally this is called exit_status, cloud-init sends this as
        # result, using the StatusHandler, and previously the commissioning
        # scripts sent this as script_result.
        for exit_status_name in ['exit_status', 'script_result', 'result']:
            exit_status = get_optional_param(
                request, exit_status_name, None, Int)
            if exit_status is not None:
                break
        if exit_status is None:
            exit_status = 0

        results[script_result] = {
            'exit_status': exit_status,
            key: content,
        }


class MetadataViewHandler(OperationsHandler):
    create = update = delete = None

    def read(self, request, mac=None):
        return make_list_response(sorted(self.subfields))


class IndexHandler(MetadataViewHandler):
    """Top-level metadata listing."""

    subfields = ('latest', '2012-03-01')


class StatusHandler(MetadataViewHandler):
    read = update = delete = None

    def create(self, request, system_id):
        """Receive and process a status message from a node, usally cloud-init.

        A node can call this to report progress of its booting or deployment.

        Calling this from a node that is not Allocated, Commissioning, Ready,
        or Failed Tests will update the substatus_message node attribute.
        Signaling completion more than once is not an error; all but the first
        successful call are ignored.

        This method accepts a single JSON-encoded object payload, described as
        follows.

        {
            "event_type": "finish",
            "origin": "curtin",
            "description": "Finished XYZ",
            "name": "cmd-install",
            "result": "SUCCESS",
            "files": [
                {
                    "name": "logs.tgz",
                    "encoding": "base64",
                    "content": "QXVnIDI1IDA3OjE3OjAxIG1hYXMtZGV2...
                },
                {
                    "name": "results.log",
                    "compression": "bzip2"
                    "encoding": "base64",
                    "content": "AAAAAAAAAAAAAAAAAAAAAAA...
                }
            ]
        }

        `event_type` can be "start", "progress" or "finish".

        `origin` tells us the program that originated the call.

        `description` is a human-readable, operator-friendly string that
        conveys what is being done to the node and that can be presented on the
        web UI.

        `name` is the name of the activity that's being executed. It's
        meaningful to the calling program and is a slash-separated path. We are
        mainly concerned with top-level events (no slashes), which are used to
        change the status of the node.

        `result` can be "SUCCESS" or "FAILURE" indicating whether the activity
        was successful or not.

        `files`, when present, contains one or more files. The attribute `path`
        tells us the name of the file, `compression` tells the compression we
        used before applying the `encoding` and content is the encoded data
        from the file. If the file being sent is the result of the execution of
        a script, the `result` key will hold its value. If `result` is not
        sent, it is interpreted as zero.

        `script_result_id`, when present, MAAS will search for an existing
        ScriptResult with the given id to store files present.

        """

        def _retrieve_content(compression, encoding, content):
            """Extract the content of the sent file."""
            # Select the appropriate decompressor.
            if compression is None:
                decompress = lambda s: s
            elif compression == 'bzip2':
                decompress = bz2.decompress
            else:
                raise MAASAPIBadRequest(
                    'Invalid compression: %s' % sent_file['compression'])

            # Select the appropriate decoder.
            if encoding == 'base64':
                decode = base64.decodebytes
            else:
                raise MAASAPIBadRequest(
                    'Invalid encoding: %s' % sent_file['encoding'])

            content = sent_file['content'].encode("ascii")
            return decompress(decode(content))

        def _is_top_level(activity_name):
            """Top-level events do not have slashes in theit names."""
            return '/' not in activity_name

        node = get_queried_node(request)
        payload = request.read()

        try:
            payload = payload.decode("ascii")
        except UnicodeDecodeError as error:
            message = "Status payload must be ASCII-only: %s" % error
            logger.error(message)
            raise MAASAPIBadRequest(message)

        try:
            message = json.loads(payload)
        except ValueError:
            message = "Status payload is not valid JSON:\n%s\n\n" % payload
            logger.error(message)
            raise MAASAPIBadRequest(message)

        # Mandatory attributes.
        try:
            event_type = message['event_type']
            origin = message['origin']
            activity_name = message['name']
            description = message['description']
        except KeyError:
            message = 'Missing parameter in status message %s' % payload
            logger.error(message)
            raise MAASAPIBadRequest(message)

        # Optional attributes.
        result = get_optional_param(message, 'result', None, String)

        # Add this event to the node event log.
        add_event_to_node_event_log(
            node, origin, activity_name, description, result)

        # Group files together with the ScriptResult they belong.
        results = {}
        for sent_file in message.get('files', []):
            # Set the result type according to the node's status.
            if (node.status == NODE_STATUS.COMMISSIONING or
                    node.node_type != NODE_TYPE.MACHINE):
                script_set = node.current_commissioning_script_set
            elif node.status == NODE_STATUS.DEPLOYING:
                script_set = node.current_installation_script_set
            else:
                raise MAASAPIBadRequest(
                    "Invalid status for saving files: %d" % node.status)

            script_name = get_mandatory_param(sent_file, 'path', String)
            content = _retrieve_content(
                compression=get_optional_param(
                    sent_file, 'compression', None, String),
                encoding=get_mandatory_param(sent_file, 'encoding', String),
                content=get_mandatory_param(sent_file, 'content', String))
            process_file(results, script_set, script_name, content, sent_file)

        # Commit results to the database.
        for script_result, args in results.items():
            script_result.store_result(**args)

        # At the end of a top-level event, we change the node status.
        if _is_top_level(activity_name) and event_type == 'finish':
            if node.status == NODE_STATUS.COMMISSIONING:
                if result in ['FAIL', 'FAILURE']:
                    node.status = NODE_STATUS.FAILED_COMMISSIONING

            elif node.status == NODE_STATUS.DEPLOYING:
                if result in ['FAIL', 'FAILURE']:
                    node.mark_failed(
                        comment="Installation failed (refer to the "
                                "installation log for more information).")
            elif node.status == NODE_STATUS.DISK_ERASING:
                if result in ['FAIL', 'FAILURE']:
                    node.mark_failed(comment="Failed to erase disks.")

            # Deallocate the node if we enter any terminal state.
            if node.node_type == NODE_TYPE.MACHINE and node.status in [
                    NODE_STATUS.READY,
                    NODE_STATUS.FAILED_COMMISSIONING]:
                node.status_expires = None
                node.owner = None
                node.error = 'failed: %s' % description

        node.save()
        return rc.ALL_OK


class VersionIndexHandler(MetadataViewHandler):
    """Listing for a given metadata version."""
    create = update = delete = None
    subfields = ('maas-commissioning-scripts', 'meta-data', 'user-data')

    # States in which a node is allowed to signal
    # commissioning/installing/entering-rescue-mode status.
    # (Only in Commissioning/Deploying/EnteringRescueMode state, however,
    # will it have any effect.)
    signalable_states = [
        NODE_STATUS.BROKEN,
        NODE_STATUS.COMMISSIONING,
        NODE_STATUS.FAILED_COMMISSIONING,
        NODE_STATUS.DEPLOYING,
        NODE_STATUS.FAILED_DEPLOYMENT,
        NODE_STATUS.READY,
        NODE_STATUS.DISK_ERASING,
        NODE_STATUS.ENTERING_RESCUE_MODE,
        NODE_STATUS.FAILED_ENTERING_RESCUE_MODE,
        ]

    effective_signalable_states = [
        NODE_STATUS.COMMISSIONING,
        NODE_STATUS.DEPLOYING,
        NODE_STATUS.DISK_ERASING,
        NODE_STATUS.ENTERING_RESCUE_MODE,
    ]

    # Statuses that a commissioning node may signal, and the respective
    # state transitions that they trigger on the node.
    signaling_statuses = {
        SIGNAL_STATUS.OK: NODE_STATUS.READY,
        SIGNAL_STATUS.FAILED: NODE_STATUS.FAILED_COMMISSIONING,
        SIGNAL_STATUS.WORKING: None,
    }

    def read(self, request, version, mac=None):
        """Read the metadata index for this version."""
        check_version(version)
        node = get_queried_node(request, for_mac=mac)
        if NodeUserData.objects.has_user_data(node):
            shown_subfields = self.subfields
        else:
            shown_subfields = list(self.subfields)
            shown_subfields.remove('user-data')
        return make_list_response(sorted(shown_subfields))

    def _store_results(self, node, script_set, request):
        """Store uploaded results."""
        # Group files together with the ScriptResult they belong.
        results = {}
        for script_name, uploaded_file in request.FILES.items():
            content = uploaded_file.read()
            process_file(
                results, script_set, script_name, content, request.POST)

        # Commit results to the database.
        for script_result, args in results.items():
            script_result.store_result(**args)

    @operation(idempotent=False)
    def signal(self, request, version=None, mac=None):
        """Signal commissioning/installation/entering-rescue-mode status.

        A node booted into an ephemeral environment can call this to report
        progress of any scripts given to it by MAAS.

        Calling this from a node that is not Allocated, Commissioning, Ready,
        Broken, Deployed, or Failed Tests is an error. Signaling
        completion more than once is not an error; all but the first
        successful call are ignored.

        :param status: A commissioning/installation/entering-rescue-mode
            status code.
            This can be "OK" (to signal that
            commissioning/installation/entering-rescue-mode has completed
            successfully), or "FAILED" (to signal failure), or
            "WORKING" (for progress reports).
        :param error: An optional error string. If given, this will be stored
            (overwriting any previous error string), and displayed in the MAAS
            UI. If not given, any previous error string will be cleared.
        :param script_result_id: What ScriptResult this signal is for. If the
            signal contains a file upload the id will be used to find the
            ScriptResult row.
        :param exit_status: The return code of the script run.
        """
        node = get_queried_node(request, for_mac=mac)
        status = get_mandatory_param(request.POST, 'status', String)
        target_status = None
        if (node.status not in self.signalable_states and
                node.node_type == NODE_TYPE.MACHINE):
            raise NodeStateViolation(
                "Machine wasn't commissioning/installing/entering-rescue-mode "
                "(status is %s)" % NODE_STATUS_CHOICES_DICT[node.status])

        # These statuses are acceptable for commissioning, disk erasing,
        # entering rescue mode and deploying.
        if (status not in self.signaling_statuses and
                node.node_type == NODE_TYPE.MACHINE):
            raise MAASAPIBadRequest(
                "Unknown commissioning/installation/entering-rescue-mode "
                "status: '%s'" % status)

        if (node.status not in self.effective_signalable_states and
                node.node_type == NODE_TYPE.MACHINE):
            # If commissioning, it is already registered.  Nothing to be done.
            # If it is installing, should be in deploying state.
            return rc.ALL_OK

        if (node.status == NODE_STATUS.COMMISSIONING or
                node.node_type != NODE_TYPE.MACHINE):

            # Store the commissioning results.
            self._store_results(
                node, node.current_commissioning_script_set, request)

            # This is skipped when its the rack controller using this endpoint.
            if node.node_type not in (
                    NODE_TYPE.RACK_CONTROLLER,
                    NODE_TYPE.REGION_AND_RACK_CONTROLLER):

                # Commissioning was successful setup the default storage layout
                # and the initial networking configuration for the node.
                if status == SIGNAL_STATUS.OK:
                    # XXX 2016-05-10 ltrager, LP:1580405 - Exceptions raised
                    # here are not logged or shown to the user.
                    node.set_default_storage_layout()
                    node.set_initial_networking_configuration()

                # XXX 2014-10-21 newell, bug=1382075
                # Auto detection for IPMI tries to save power parameters
                # for Moonshot.  This causes issues if the node's power type
                # is already MSCM as it uses SSH instead of IPMI.  This fix
                # is temporary as power parameters should not be overwritten
                # during commissioning because MAAS already has knowledge to
                # boot the node.
                # See MP discussion bug=1389808, for further details on why
                # we are using bug fix 1382075 here.
                if node.power_type != "mscm":
                    store_node_power_parameters(node, request)

            target_status = self.signaling_statuses.get(status)
            if target_status in [NODE_STATUS.FAILED_COMMISSIONING,
               NODE_STATUS.READY]:
                node.status_expires = None
            # Recalculate tags when commissioning ends.
            if target_status == NODE_STATUS.READY:
                populate_tags_for_single_node(Tag.objects.all(), node)

        elif node.status == NODE_STATUS.DEPLOYING:
            self._store_results(
                node, node.current_installation_script_set, request)
            if status == SIGNAL_STATUS.FAILED:
                node.mark_failed(
                    comment="Installation failed (refer to the "
                            "installation log for more information).")
            target_status = None
        elif node.status == NODE_STATUS.DISK_ERASING:
            if status == SIGNAL_STATUS.OK:
                # disk erasing complete, release node
                node.release()
            elif status == SIGNAL_STATUS.FAILED:
                node.mark_failed(comment="Failed to erase disks.")
            target_status = None
        elif node.status == NODE_STATUS.ENTERING_RESCUE_MODE:
            if status == SIGNAL_STATUS.OK:
                # entering rescue mode completed, set status
                target_status = NODE_STATUS.RESCUE_MODE
            elif status == SIGNAL_STATUS.FAILED:
                node.mark_failed(comment="Failed to enter rescue mode.")
                target_status = None
        if target_status in (None, node.status):
            # No status change.  Nothing to be done.
            return rc.ALL_OK

        if node.node_type == NODE_TYPE.MACHINE:
            node.status = target_status
            # When moving to a terminal state, remove the allocation
            # if not in rescue mode.
            if node.status != NODE_STATUS.RESCUE_MODE:
                node.owner = None
        node.error = get_optional_param(request.POST, 'error', '', String)

        # Done.
        node.save()
        return rc.ALL_OK

    @operation(idempotent=False)
    def netboot_off(self, request, version=None, mac=None):
        """Turn off netboot on the node.

        A deploying node can call this to turn off netbooting when
        it finishes installing itself.
        """
        node = get_queried_node(request, for_mac=mac)
        node.set_netboot(False)
        return rc.ALL_OK

    @operation(idempotent=False)
    def netboot_on(self, request, version=None, mac=None):
        """Turn on netboot on the node."""
        node = get_queried_node(request, for_mac=mac)
        node.set_netboot(True)
        return rc.ALL_OK


class MetaDataHandler(VersionIndexHandler):
    """Meta-data listing for a given version."""

    subfields = (
        'instance-id',
        'local-hostname',
        'public-keys',
        'vendor-data',
        'x509',
    )

    def get_attribute_producer(self, item):
        """Return a callable to deliver a given metadata item.

        :param item: Sub-path for the attribute, e.g. "local-hostname" to
            get a handler that returns the logged-in node's hostname.
        :type item: unicode
        :return: A callable that accepts as arguments the logged-in node;
            the requested metadata version (e.g. "latest"); and `item`.  It
            returns an HttpResponse.
        :rtype: Callable
        """
        subfield = item.split('/')[0]
        if subfield not in self.subfields:
            raise MAASAPINotFound("Unknown metadata attribute: %s" % subfield)

        producers = {
            'instance-id': self.instance_id,
            'local-hostname': self.local_hostname,
            'public-keys': self.public_keys,
            'vendor-data': self.vendor_data,
            'x509': self.ssl_certs,
        }

        return producers[subfield]

    def read(self, request, version, mac=None, item=None):
        check_version(version)
        node = get_queried_node(request, for_mac=mac)

        # Requesting the list of attributes, not any particular
        # attribute.
        if item is None or len(item) == 0:
            subfields = list(self.subfields)
            commissioning_without_ssh = (
                node.status == NODE_STATUS.COMMISSIONING and
                not node.enable_ssh)
            # Add public-keys to the list of attributes, if the
            # node has registered SSH keys.
            keys = SSHKey.objects.get_keys_for_user(user=node.owner)
            if not keys or commissioning_without_ssh:
                subfields.remove('public-keys')
            return make_list_response(sorted(subfields))

        producer = self.get_attribute_producer(item)
        return producer(node, version, item)

    def local_hostname(self, node, version, item):
        """Produce local-hostname attribute."""
        return make_text_response(node.fqdn)

    def instance_id(self, node, version, item):
        """Produce instance-id attribute."""
        return make_text_response(node.system_id)

    def vendor_data(self, node, version, item):
        vendor_data = {"cloud-init": "#cloud-config\n%s" % yaml.safe_dump(
            get_vendor_data(node)
        )}
        vendor_data_dump = yaml.safe_dump(
            vendor_data, encoding="utf-8", default_flow_style=False)
        # Use the same Content-Type as Piston 3 for YAML content.
        return HttpResponse(
            vendor_data_dump, content_type="application/x-yaml; charset=utf-8")

    def public_keys(self, node, version, item):
        """ Produce public-keys attribute."""
        return make_list_response(
            SSHKey.objects.get_keys_for_user(user=node.owner))

    def ssl_certs(self, node, version, item):
        """ Produce x509 certs attribute. """
        return make_list_response(
            SSLKey.objects.get_keys_for_user(user=node.owner))


class UserDataHandler(MetadataViewHandler):
    """User-data blob for a given version."""

    def read(self, request, version, mac=None):
        check_version(version)
        node = get_queried_node(request, for_mac=mac)
        try:
            # When a node is deploying, cloud-init's request
            # for user-data is when MAAS hands the node
            # off to a user.
            if node.status == NODE_STATUS.DEPLOYING:
                node.end_deployment()
            # If this node is supposed to be powered off, serve the
            # 'poweroff' userdata.
            if node.get_boot_purpose() == 'poweroff':
                user_data = poweroff.generate_user_data(node=node)
            else:
                user_data = NodeUserData.objects.get_user_data(node)
            return HttpResponse(
                user_data, content_type='application/octet-stream')
        except NodeUserData.DoesNotExist:
            logger.info(
                "No user data registered for node named %s" % node.hostname)
            return HttpResponse(status=int(http.client.NOT_FOUND))


class CurtinUserDataHandler(MetadataViewHandler):
    """Curtin user-data blob for a given version."""

    def read(self, request, version, mac=None):
        check_version(version)
        node = get_queried_node(request, for_mac=mac)
        user_data = get_curtin_userdata(node)
        return HttpResponse(
            user_data,
            content_type='application/octet-stream')


class CommissioningScriptsHandler(MetadataViewHandler):
    """Return a tar archive containing the commissioning scripts."""

    def _iter_builtin_scripts(self):
        for script in NODE_INFO_SCRIPTS.values():
            yield script['name'], script['content']

    def _iter_user_scripts(self):
        for script in Script.objects.filter(
                script_type=SCRIPT_TYPE.COMMISSIONING):
            try:
                # Check if the script is a base64 encoded binary.
                content = base64.b64decode(script.script.data)
            except:
                # If it isn't encode the text as binary data.
                content = script.script.data.encode()
            yield script.name, content

    def _iter_scripts(self):
        return chain(
            self._iter_builtin_scripts(),
            self._iter_user_scripts(),
        )

    def _get_archive(self):
        """Produce a tar archive of all commissionig scripts.

        Each of the scripts will be in the `ARCHIVE_PREFIX` directory.
        """
        binary = BytesIO()
        scripts = sorted(self._iter_scripts())
        with tarfile.open(mode='w', fileobj=binary) as tarball:
            add_script = partial(
                add_script_to_archive, tarball, mtime=time.time())
            for name, content in scripts:
                add_script(name, content)
        return binary.getvalue()

    def read(self, request, version, mac=None):
        check_version(version)
        return HttpResponse(
            self._get_archive(), content_type='application/tar')


class EnlistMetaDataHandler(OperationsHandler):
    """this has to handle the 'meta-data' portion of the meta-data api
    for enlistment only.  It should mimic the read-only portion
    of /VersionIndexHandler"""

    create = update = delete = None

    data = {
        'instance-id': 'i-maas-enlistment',
        'local-hostname': "maas-enlisting-node",
        'public-keys': "",
    }

    def read(self, request, version, item=None):
        check_version(version)

        # Requesting the list of attributes, not any particular attribute.
        if item is None or len(item) == 0:
            keys = sorted(self.data.keys())
            # There's nothing in public-keys, so we don't advertise it.
            # But cloud-init does ask for it and it's not worth logging
            # a traceback for.
            keys.remove('public-keys')
            return make_list_response(keys)

        if item not in self.data:
            raise MAASAPINotFound("Unknown metadata attribute: %s" % item)

        return make_text_response(self.data[item])


class EnlistUserDataHandler(OperationsHandler):
    """User-data for the enlistment environment"""

    def read(self, request, version):
        check_version(version)
        rack_controller = find_rack_controller(request)
        # XXX: Set a charset for text/plain. Django automatically encodes
        # non-binary content using DEFAULT_CHARSET (which is UTF-8 by default)
        # but only sets the charset parameter in the content-type header when
        # a content-type is NOT provided.
        return HttpResponse(
            get_enlist_userdata(rack_controller=rack_controller),
            content_type="text/plain")


class EnlistVersionIndexHandler(OperationsHandler):
    create = update = delete = None
    subfields = ('meta-data', 'user-data')

    def read(self, request, version):
        return make_list_response(sorted(self.subfields))


class AnonMetaDataHandler(VersionIndexHandler):
    """Anonymous metadata."""

    @operation(idempotent=True)
    def get_enlist_preseed(self, request, version=None):
        """Render and return a preseed script for enlistment."""
        rack_controller = find_rack_controller(request)
        # XXX: Set a charset for text/plain. Django automatically encodes
        # non-binary content using DEFAULT_CHARSET (which is UTF-8 by default)
        # but only sets the charset parameter in the content-type header when
        # a content-type is NOT provided.
        return HttpResponse(
            get_enlist_preseed(rack_controller=rack_controller),
            content_type="text/plain")

    @operation(idempotent=True)
    def get_preseed(self, request, version=None, system_id=None):
        """Render and return a preseed script for the given node."""
        node = get_object_or_404(Node, system_id=system_id)
        # XXX: Set a charset for text/plain. Django automatically encodes
        # non-binary content using DEFAULT_CHARSET (which is UTF-8 by default)
        # but only sets the charset parameter in the content-type header when
        # a content-type is NOT provided.
        return HttpResponse(get_preseed(node), content_type="text/plain")

    @operation(idempotent=False)
    def netboot_off(self, request, version=None, system_id=None):
        """Turn off netboot on the node.

        A commissioning node can call this to turn off netbooting when
        it finishes installing itself.
        """
        node = get_object_or_404(Node, system_id=system_id)
        node.set_netboot(False)

        # Build and register an event for "node installation finished".
        # This is a best-guess. At the moment, netboot_off() only gets
        # called when the node has finished installing, so it's an
        # accurate predictor of the end of the install process.
        type_name = EVENT_TYPES.NODE_INSTALLATION_FINISHED
        event_details = EVENT_DETAILS[type_name]
        Event.objects.register_event_and_event_type(
            node.system_id, type_name, type_level=event_details.level,
            type_description=event_details.description,
            event_description="Node disabled netboot")
        return rc.ALL_OK
