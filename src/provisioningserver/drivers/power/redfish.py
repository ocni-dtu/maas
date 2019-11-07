# Copyright 2018-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Redfish Power Driver."""

__all__ = ["RedfishPowerDriver"]

from base64 import b64encode
from http import HTTPStatus
from io import BytesIO
import json
from os.path import basename, join

from provisioningserver.drivers import (
    make_ip_extractor,
    make_setting_field,
    SETTING_SCOPE,
)
from provisioningserver.drivers.power import PowerActionError, PowerDriver
from provisioningserver.drivers.power.utils import WebClientContextFactory
from provisioningserver.utils.twisted import asynchronous
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.web.client import (
    Agent,
    FileBodyProducer,
    PartialDownloadError,
    readBody,
    RedirectAgent,
)
from twisted.web.http_headers import Headers

# no trailing slashes
REDFISH_POWER_CONTROL_ENDPOINT = (
    b"redfish/v1/Systems/%s/Actions/ComputerSystem.Reset"
)

REDFISH_SYSTEMS_ENDPOINT = b"redfish/v1/Systems"


class RedfishPowerDriverBase(PowerDriver):
    def get_url(self, context):
        """Return url for the pod."""
        url = context.get("power_address")
        if "https" not in url and "http" not in url:
            # Prepend https
            url = join("https://", url)
        return url.encode("utf-8")

    def make_auth_headers(self, power_user, power_pass, **kwargs):
        """Return authentication headers."""
        creds = "%s:%s" % (power_user, power_pass)
        authorization = b64encode(creds.encode("utf-8"))
        return Headers(
            {
                b"User-Agent": [b"MAAS"],
                b"Accept": [b"application/json"],
                b"Authorization": [b"Basic " + authorization],
                b"Content-Type": [b"application/json; charset=utf-8"],
            }
        )

    @asynchronous
    def redfish_request(self, method, uri, headers=None, bodyProducer=None):
        """Send the redfish request and return the response."""
        agent = RedirectAgent(
            Agent(reactor, contextFactory=WebClientContextFactory())
        )
        d = agent.request(
            method, uri, headers=headers, bodyProducer=bodyProducer
        )

        def render_response(response):
            """Render the HTTPS response received."""

            def eb_catch_partial(failure):
                # Twisted is raising PartialDownloadError because the responses
                # do not contain a Content-Length header. Since every response
                # holds the whole body we just take the result.
                failure.trap(PartialDownloadError)
                if int(failure.value.status) == HTTPStatus.OK:
                    return failure.value.response
                else:
                    return failure

            def cb_json_decode(data):
                data = data.decode("utf-8")
                # Only decode non-empty response bodies.
                if data:
                    # occasionally invalid json is returned. provide a clear
                    # error in that case
                    try:
                        return json.loads(data)
                    except ValueError as error:
                        raise PowerActionError(
                            "Redfish request failed from a JSON parse error:"
                            " %s." % error
                        )

            def cb_attach_headers(data, headers):
                return data, headers

            # Error out if the response has a status code of 400 or above.
            if response.code >= int(HTTPStatus.BAD_REQUEST):
                # if there was no trailing slash, retry with a trailing slash
                # because of varying requirements of BMC manufacturers
                if (
                    response.code == HTTPStatus.NOT_FOUND
                    and uri.decode("utf-8")[-1] != "/"
                ):
                    d = agent.request(
                        method,
                        uri + "/".encode("utf-8"),
                        headers=headers,
                        bodyProducer=bodyProducer,
                    )
                else:
                    raise PowerActionError(
                        "Redfish request failed with response status code:"
                        " %s." % response.code
                    )

            d = readBody(response)
            d.addErrback(eb_catch_partial)
            d.addCallback(cb_json_decode)
            d.addCallback(cb_attach_headers, headers=response.headers)
            return d

        d.addCallback(render_response)
        return d


class RedfishPowerDriver(RedfishPowerDriverBase):

    chassis = True  # Redfish API endpoints can be probed and enlisted.

    name = "redfish"
    description = "Redfish"
    settings = [
        make_setting_field("power_address", "Redfish address", required=True),
        make_setting_field("power_user", "Redfish user", required=True),
        make_setting_field(
            "power_pass",
            "Redfish password",
            field_type="password",
            required=True,
        ),
        make_setting_field("node_id", "Node ID", scope=SETTING_SCOPE.NODE),
    ]
    ip_extractor = make_ip_extractor("power_address")

    def detect_missing_packages(self):
        # no required packages
        return []

    @inlineCallbacks
    def process_redfish_context(self, context):
        """Process Redfish power driver context.

        Returns the basename of the first member found
        in the Redfish Systems:

        "Members": [
          {
            "@odata.id": "/redfish/v1/Systems/1"
          }
        """
        url = self.get_url(context)
        headers = self.make_auth_headers(**context)
        node_id = context.get("node_id")
        if node_id:
            node_id = node_id.encode("utf-8")
        else:
            node_id = yield self.get_node_id(url, headers)
        return url, node_id, headers

    @inlineCallbacks
    def get_node_id(self, url, headers):
        uri = join(url, REDFISH_SYSTEMS_ENDPOINT)
        systems, _ = yield self.redfish_request(b"GET", uri, headers)
        members = systems.get("Members")
        member = members[0].get("@odata.id")
        return basename(member).encode("utf-8")

    @inlineCallbacks
    def set_pxe_boot(self, url, node_id, headers):
        """Set the machine with node_id to PXE boot."""
        endpoint = join(REDFISH_SYSTEMS_ENDPOINT, b"%s" % node_id)
        payload = FileBodyProducer(
            BytesIO(
                json.dumps(
                    {
                        "Boot": {
                            "BootSourceOverrideEnabled": "Once",
                            "BootSourceOverrideTarget": "Pxe",
                        }
                    }
                ).encode("utf-8")
            )
        )
        yield self.redfish_request(
            b"PATCH", join(url, endpoint), headers, payload
        )

    @inlineCallbacks
    def power(self, power_change, url, node_id, headers):
        """Issue `power` command."""
        endpoint = REDFISH_POWER_CONTROL_ENDPOINT % node_id
        payload = FileBodyProducer(
            BytesIO(
                json.dumps(
                    {"Action": "Reset", "ResetType": "%s" % power_change}
                ).encode("utf-8")
            )
        )
        yield self.redfish_request(
            b"POST", join(url, endpoint), headers, payload
        )

    @asynchronous
    @inlineCallbacks
    def power_on(self, node_id, context):
        """Power on machine."""
        url, node_id, headers = yield self.process_redfish_context(context)
        power_state = yield self.power_query(node_id, context)
        # Power off the machine if currently on.
        if power_state == "on":
            yield self.power("ForceOff", url, node_id, headers)
        # Set to PXE boot.
        yield self.set_pxe_boot(url, node_id, headers)
        # Power on the machine.
        yield self.power("On", url, node_id, headers)

    @asynchronous
    @inlineCallbacks
    def power_off(self, node_id, context):
        """Power off machine."""
        url, node_id, headers = yield self.process_redfish_context(context)
        # Set to PXE boot.
        yield self.set_pxe_boot(url, node_id, headers)
        # Power off the machine if it is not already off
        power_state = yield self.power_query(node_id, context)
        if power_state != "off":
            yield self.power("ForceOff", url, node_id, headers)

    @asynchronous
    @inlineCallbacks
    def power_query(self, node_id, context):
        """Power query machine."""
        url, node_id, headers = yield self.process_redfish_context(context)
        uri = join(url, REDFISH_SYSTEMS_ENDPOINT, b"%s" % node_id)
        node_data, _ = yield self.redfish_request(b"GET", uri, headers)
        return node_data.get("PowerState").lower()
