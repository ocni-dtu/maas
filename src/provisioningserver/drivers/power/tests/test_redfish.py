# Copyright 2018-2019 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.power.redfish`."""

__all__ = []


from base64 import b64encode
from copy import deepcopy
from http import HTTPStatus
from io import BytesIO
import json
from os.path import join
import random
from unittest.mock import call, Mock

from maastesting.factory import factory
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase, MAASTwistedRunTest
from provisioningserver.drivers.power import PowerActionError
from provisioningserver.drivers.power.redfish import (
    REDFISH_POWER_CONTROL_ENDPOINT,
    RedfishPowerDriver,
    WebClientContextFactory,
)
import provisioningserver.drivers.power.redfish as redfish_module
from testtools import ExpectedException
from twisted.internet._sslverify import ClientTLSOptions
from twisted.internet.defer import fail, inlineCallbacks, succeed
from twisted.web.client import FileBodyProducer, PartialDownloadError
from twisted.web.http_headers import Headers


SAMPLE_JSON_SYSTEMS = {
    "@odata.context": "/redfish/v1/$metadata#Systems",
    "@odata.count": 1,
    "@odata.id": "/redfish/v1/Systems",
    "@odata.type": "#ComputerSystem.1.0.0.ComputerSystemCollection",
    "Description": "Collection of Computer Systems",
    "Members": [{"@odata.id": "/redfish/v1/Systems/1"}],
    "Name": "Computer System Collection",
}

SAMPLE_JSON_SYSTEM = {
    "@odata.context": "/redfish/v1/$metadata#Systems/Members/$entity",
    "@odata.id": "/redfish/v1/Systems/1",
    "@odata.type": "#ComputerSystem.1.0.0.ComputerSystem",
    "Actions": {
        "#ComputerSystem.Reset": {
            "ResetType@Redfish.AllowableValues": [
                "On",
                "ForceOff",
                "GracefulRestart",
                "PushPowerButton",
                "Nmi",
            ],
            "target": "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset",
        }
    },
    "AssetTag": "",
    "BiosVersion": "2.1.7",
    "Boot": {
        "BootSourceOverrideEnabled": "Once",
        "BootSourceOverrideTarget": "None",
        "BootSourceOverrideTarget@Redfish.AllowableValues": [
            "None",
            "Pxe",
            "Floppy",
            "Cd",
            "Hdd",
            "BiosSetup",
            "Utilities",
            "UefiTarget",
        ],
        "UefiTargetBootSourceOverride": "",
    },
    "Description": "Computer System which represents a machine.",
    "EthernetInterfaces": {
        "@odata.id": "/redfish/v1/Systems/1/EthernetInterfaces"
    },
    "HostName": "WORTHY-BOAR",
    "Id": "1",
    "IndicatorLED": "Off",
    "Links": {
        "Chassis": [{"@odata.id": "/redfish/v1/Chassis/1"}],
        "ManagedBy": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1"}],
        "PoweredBy": [
            {"@odata.id": "/redfish/v1/Chassis/1/Power/PowerSupplies/..."},
            {"@odata.id": "/redfish/v1/Chassis/1/Power/PowerSupplies/..."},
        ],
    },
    "Manufacturer": "Dell Inc.",
    "MemorySummary": {
        "Status": {"Health": "OK", "HealthRollUp": "OK", "State": "Enabled"},
        "TotalSystemMemoryGiB": 64,
    },
    "Model": "PowerEdge R630",
    "Name": "System",
    "PartNumber": "02C2CPA01",
    "PowerState": "Off",
    "ProcessorSummary": {
        "Count": 2,
        "Model": "Intel(R) Xeon(R) CPU E5-2667 v4 @ 3.20GHz",
        "Status": {
            "Health": "Critical",
            "HealthRollUp": "Critical",
            "State": "Enabled",
        },
    },
    "Processors": {"@odata.id": "/redfish/v1/Systems/1/Processors"},
    "SKU": "7PW1RD2",
    "SerialNumber": "CN7475166I0364",
    "SimpleStorage": {
        "@odata.id": "/redfish/v1/Systems/1/Storage/Controllers"
    },
    "Status": {
        "Health": "Critical",
        "HealthRollUp": "Critical",
        "State": "Offline",
    },
    "SystemType": "Physical",
    "UUID": "4c4c4544-0050-5710-8031-b7c04f524432",
}


def make_context():
    return {
        "power_address": factory.make_ipv4_address(),
        "power_user": factory.make_name("power_user"),
        "power_pass": factory.make_name("power_pass"),
    }


class TestWebClientContextFactory(MAASTestCase):
    def test_creatorForNetloc_returns_tls_options(self):
        hostname = factory.make_name("hostname").encode("utf-8")
        port = random.randint(1000, 2000)
        contextFactory = WebClientContextFactory()
        opts = contextFactory.creatorForNetloc(hostname, port)
        self.assertIsInstance(opts, ClientTLSOptions)


class TestRedfishPowerDriver(MAASTestCase):

    run_tests_with = MAASTwistedRunTest.make_factory(timeout=5)

    def test_missing_packages(self):
        # there's nothing to check for, just confirm it returns []
        driver = RedfishPowerDriver()
        missing = driver.detect_missing_packages()
        self.assertItemsEqual([], missing)

    def test_get_url_with_ip(self):
        driver = RedfishPowerDriver()
        context = make_context()
        ip = context.get("power_address").encode("utf-8")
        expected_url = b"https://%s" % ip
        url = driver.get_url(context)
        self.assertEqual(expected_url, url)

    def test_get_url_with_https(self):
        driver = RedfishPowerDriver()
        context = make_context()
        context["power_address"] = join("https://", context["power_address"])
        expected_url = context.get("power_address").encode("utf-8")
        url = driver.get_url(context)
        self.assertEqual(expected_url, url)

    def test_get_url_with_http(self):
        driver = RedfishPowerDriver()
        context = make_context()
        context["power_address"] = join("http://", context["power_address"])
        expected_url = context.get("power_address").encode("utf-8")
        url = driver.get_url(context)
        self.assertEqual(expected_url, url)

    def test__make_auth_headers(self):
        power_user = factory.make_name("power_user")
        power_pass = factory.make_name("power_pass")
        creds = "%s:%s" % (power_user, power_pass)
        authorization = b64encode(creds.encode("utf-8"))
        attributes = {
            b"User-Agent": [b"MAAS"],
            b"Accept": [b"application/json"],
            b"Authorization": [b"Basic " + authorization],
            b"Content-Type": [b"application/json; charset=utf-8"],
        }
        driver = RedfishPowerDriver()
        headers = driver.make_auth_headers(power_user, power_pass)
        self.assertEquals(headers, Headers(attributes))

    @inlineCallbacks
    def test_redfish_request_renders_response(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.OK
        expected_headers.headers = "Testing Headers"
        mock_agent.return_value.request.return_value = succeed(
            expected_headers
        )
        mock_readBody = self.patch(redfish_module, "readBody")
        mock_readBody.return_value = succeed(
            json.dumps(SAMPLE_JSON_SYSTEMS).encode("utf-8")
        )
        expected_response = SAMPLE_JSON_SYSTEMS

        response, headers = yield driver.redfish_request(b"GET", uri, headers)
        self.assertEquals(expected_response, response)
        self.assertEquals(expected_headers.headers, headers)

    @inlineCallbacks
    def test_wrap_redfish_request_retries_404s_trailing_slash(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.NOT_FOUND
        expected_headers.headers = "Testing Headers"
        happy_headers = Mock()
        happy_headers.code = HTTPStatus.OK
        happy_headers.headers = "Testing Headers"
        mock_agent.return_value.request.side_effect = [
            succeed(expected_headers),
            succeed(happy_headers),
        ]
        mock_readBody = self.patch(redfish_module, "readBody")
        mock_readBody.return_value = succeed(
            json.dumps(SAMPLE_JSON_SYSTEMS).encode("utf-8")
        )
        expected_response = SAMPLE_JSON_SYSTEMS

        response, return_headers = yield driver.redfish_request(
            b"GET", uri, headers
        )
        self.assertThat(
            mock_agent.return_value.request,
            MockCallsMatch(
                call(b"GET", uri, headers, None),
                call(b"GET", uri + "/".encode("utf-8"), headers, None),
            ),
        )
        self.assertEquals(expected_response, response)
        self.assertEquals(expected_headers.headers, return_headers)

    @inlineCallbacks
    def test_redfish_request_raises_invalid_json_error(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.OK
        expected_headers.headers = "Testing Headers"
        mock_agent.return_value.request.return_value = succeed(
            expected_headers
        )
        mock_readBody = self.patch(redfish_module, "readBody")
        mock_readBody.return_value = succeed(
            '{"invalid": "json"'.encode("utf-8")
        )
        with ExpectedException(PowerActionError):
            yield driver.redfish_request(b"GET", uri, headers)

    @inlineCallbacks
    def test_redfish_request_continues_partial_download_error(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.OK
        expected_headers.headers = "Testing Headers"
        mock_agent.return_value.request.return_value = succeed(
            expected_headers
        )
        mock_readBody = self.patch(redfish_module, "readBody")
        error = PartialDownloadError(
            response=json.dumps(SAMPLE_JSON_SYSTEMS).encode("utf-8"),
            code=HTTPStatus.OK,
        )
        mock_readBody.return_value = fail(error)
        expected_response = SAMPLE_JSON_SYSTEMS

        response, headers = yield driver.redfish_request(b"GET", uri, headers)
        self.assertEquals(expected_response, response)
        self.assertEquals(expected_headers.headers, headers)

    @inlineCallbacks
    def test_redfish_request_raises_failures(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.OK
        expected_headers.headers = "Testing Headers"
        mock_agent.return_value.request.return_value = succeed(
            expected_headers
        )
        mock_readBody = self.patch(redfish_module, "readBody")
        error = PartialDownloadError(
            response=json.dumps(SAMPLE_JSON_SYSTEMS).encode("utf-8"),
            code=HTTPStatus.NOT_FOUND,
        )
        mock_readBody.return_value = fail(error)

        with ExpectedException(PartialDownloadError):
            yield driver.redfish_request(b"GET", uri, headers)
        self.assertThat(mock_readBody, MockCalledOnceWith(expected_headers))

    @inlineCallbacks
    def test_redfish_request_raises_error_on_response_code_above_400(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        uri = join(url, b"redfish/v1/Systems")
        headers = driver.make_auth_headers(**context)
        mock_agent = self.patch(redfish_module, "Agent")
        mock_agent.return_value.request = Mock()
        expected_headers = Mock()
        expected_headers.code = HTTPStatus.BAD_REQUEST
        expected_headers.headers = "Testing Headers"
        mock_agent.return_value.request.return_value = succeed(
            expected_headers
        )
        mock_readBody = self.patch(redfish_module, "readBody")

        with ExpectedException(PowerActionError):
            yield driver.redfish_request(b"GET", uri, headers)
        self.assertThat(mock_readBody, MockNotCalled())

    @inlineCallbacks
    def test_power_issues_power_reset(self):
        driver = RedfishPowerDriver()
        context = make_context()
        power_change = factory.make_name("power_change")
        url = driver.get_url(context)
        headers = driver.make_auth_headers(**context)
        node_id = b"1"
        mock_file_body_producer = self.patch(
            redfish_module, "FileBodyProducer"
        )
        payload = FileBodyProducer(
            BytesIO(
                json.dumps({"ResetType": "%s" % power_change}).encode("utf-8")
            )
        )
        mock_file_body_producer.return_value = payload
        mock_redfish_request = self.patch(driver, "redfish_request")
        expected_uri = join(url, REDFISH_POWER_CONTROL_ENDPOINT % node_id)
        yield driver.power(power_change, url, node_id, headers)
        self.assertThat(
            mock_redfish_request,
            MockCalledOnceWith(b"POST", expected_uri, headers, payload),
        )

    @inlineCallbacks
    def test__set_pxe_boot(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        node_id = b"1"
        headers = driver.make_auth_headers(**context)
        mock_file_body_producer = self.patch(
            redfish_module, "FileBodyProducer"
        )
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
        mock_file_body_producer.return_value = payload
        mock_redfish_request = self.patch(driver, "redfish_request")

        yield driver.set_pxe_boot(url, node_id, headers)
        self.assertThat(
            mock_redfish_request,
            MockCalledOnceWith(
                b"PATCH",
                join(url, b"redfish/v1/Systems/%s" % node_id),
                headers,
                payload,
            ),
        )

    @inlineCallbacks
    def test__power_on(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        headers = driver.make_auth_headers(**context)
        node_id = b"1"
        mock_redfish_request = self.patch(driver, "redfish_request")
        mock_redfish_request.return_value = (SAMPLE_JSON_SYSTEMS, None)
        mock_set_pxe_boot = self.patch(driver, "set_pxe_boot")
        mock_power_query = self.patch(driver, "power_query")
        mock_power_query.return_value = "on"
        mock_power = self.patch(driver, "power")

        yield driver.power_on(node_id, context)
        self.assertThat(
            mock_set_pxe_boot, MockCalledOnceWith(url, node_id, headers)
        )
        self.assertThat(mock_power_query, MockCalledOnceWith(node_id, context))
        self.assertThat(
            mock_power,
            MockCallsMatch(
                call("ForceOff", url, node_id, headers),
                call("On", url, node_id, headers),
            ),
        )

    @inlineCallbacks
    def test__power_off(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        headers = driver.make_auth_headers(**context)
        node_id = b"1"
        mock_redfish_request = self.patch(driver, "redfish_request")
        mock_redfish_request.return_value = (SAMPLE_JSON_SYSTEMS, None)
        mock_set_pxe_boot = self.patch(driver, "set_pxe_boot")
        mock_power_query = self.patch(driver, "power_query")
        mock_power_query.return_value = "on"
        mock_power = self.patch(driver, "power")

        yield driver.power_off(node_id, context)
        self.assertThat(
            mock_set_pxe_boot, MockCalledOnceWith(url, node_id, headers)
        )
        self.assertThat(
            mock_power, MockCalledOnceWith("ForceOff", url, node_id, headers)
        )

    @inlineCallbacks
    def test__power_off_already_off(self):
        driver = RedfishPowerDriver()
        context = make_context()
        url = driver.get_url(context)
        headers = driver.make_auth_headers(**context)
        node_id = b"1"
        mock_redfish_request = self.patch(driver, "redfish_request")
        mock_redfish_request.return_value = (SAMPLE_JSON_SYSTEMS, None)
        mock_set_pxe_boot = self.patch(driver, "set_pxe_boot")
        mock_power_query = self.patch(driver, "power_query")
        mock_power_query.return_value = "off"
        mock_power = self.patch(driver, "power")

        yield driver.power_off(node_id, context)
        self.assertThat(
            mock_set_pxe_boot, MockCalledOnceWith(url, node_id, headers)
        )
        self.assertThat(mock_power, MockNotCalled())

    @inlineCallbacks
    def test_power_query_queries_on(self):
        driver = RedfishPowerDriver()
        power_change = "On"
        system_id = factory.make_name("system_id")
        context = make_context()
        mock_redfish_request = self.patch(driver, "redfish_request")
        NODE_POWERED_ON = deepcopy(SAMPLE_JSON_SYSTEM)
        NODE_POWERED_ON["PowerState"] = "On"
        mock_redfish_request.side_effect = [
            (SAMPLE_JSON_SYSTEMS, None),
            (NODE_POWERED_ON, None),
        ]
        power_state = yield driver.power_query(system_id, context)
        self.assertEquals(power_state, power_change.lower())

    @inlineCallbacks
    def test_power_query_queries_off(self):
        driver = RedfishPowerDriver()
        power_change = "Off"
        system_id = factory.make_name("system_id")
        context = make_context()
        mock_redfish_request = self.patch(driver, "redfish_request")
        mock_redfish_request.side_effect = [
            (SAMPLE_JSON_SYSTEMS, None),
            (SAMPLE_JSON_SYSTEM, None),
        ]
        power_state = yield driver.power_query(system_id, context)
        self.assertEquals(power_state, power_change.lower())
