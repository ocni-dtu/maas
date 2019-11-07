# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ``provisioningserver.utils.beaconing``."""

__all__ = []

from argparse import ArgumentParser
from collections import OrderedDict
from gzip import compress
import io
import math
import random
import struct
import subprocess
from tempfile import NamedTemporaryFile
import time
from unittest.mock import Mock
from uuid import UUID, uuid1

from maastesting.factory import factory
from maastesting.matchers import MockCalledOnceWith
from maastesting.testcase import MAASTestCase
from provisioningserver.security import fernet_encrypt_psk, MissingSharedSecret
from provisioningserver.tests.test_security import SharedSecretTestCase
from provisioningserver.utils import beaconing as beaconing_module
from provisioningserver.utils.beaconing import (
    add_arguments,
    age_out_uuid_queue,
    BEACON_HEADER_FORMAT_V1,
    beacon_to_json,
    BEACON_TYPES,
    BeaconingPacket,
    BeaconPayload,
    create_beacon_payload,
    InvalidBeaconingPacket,
    read_beacon_payload,
    run,
    uuid_to_timestamp,
)
from provisioningserver.utils.script import ActionScriptError
from testtools.matchers import (
    Contains,
    Equals,
    HasLength,
    Is,
    IsInstance,
    LessThan,
    Not,
)
from testtools.testcase import ExpectedException


class TestUUIDToTimestamp(MAASTestCase):
    def test__round_trip_preserves_timestamp(self):
        expected_timestamp = time.time()
        uuid = str(uuid1())
        actual_timestamp = uuid_to_timestamp(uuid)
        difference = math.fabs(actual_timestamp - expected_timestamp)
        # Tolerate a difference of ~3 seconds. We'll age out packets on the
        # order of minutes, so that should be good enough.
        self.assertThat(difference, LessThan(3.0))


class TestBeaconToJSON(MAASTestCase):
    """Tests for `beacon_to_json()` function."""

    def test__preserves_version_type_and_payload__discards_bytes(self):
        test_bytes = factory.make_bytes()
        test_version = factory.make_string()
        test_type = factory.make_string()
        test_payload = {factory.make_string(): factory.make_string()}
        beacon = BeaconPayload(
            test_bytes, test_version, test_type, test_payload
        )
        beacon_json = beacon_to_json(beacon)
        self.assertThat(beacon_json["version"], Equals(test_version))
        self.assertThat(beacon_json["type"], Equals(test_type))
        self.assertThat(beacon_json["payload"], Equals(test_payload))
        self.assertThat(beacon_json, Not(Contains("bytes")))
        self.assertThat(beacon_json, HasLength(3))


class TestCreateBeaconPayload(SharedSecretTestCase):
    def test__requires_maas_shared_secret_for_inner_data_payload(self):
        with ExpectedException(
            MissingSharedSecret, ".*shared secret not found.*"
        ):
            create_beacon_payload("solicitation", payload={})

    def test__returns_beaconpayload_namedtuple(self):
        beacon = create_beacon_payload("solicitation")
        self.assertThat(beacon.bytes, IsInstance(bytes))
        self.assertThat(beacon.payload, Is(None))
        self.assertThat(beacon.type, Equals("solicitation"))
        self.assertThat(beacon.version, Equals(1))

    def test__succeeds_when_shared_secret_present(self):
        self.write_secret()
        beacon = create_beacon_payload("solicitation", payload={})
        self.assertThat(beacon.type, Equals("solicitation"))
        self.assertThat(
            beacon.payload["type"], Equals(BEACON_TYPES["solicitation"])
        )

    def test__supplements_data_and_returns_complete_data(self):
        self.write_secret()
        random_type = random.choice(list(BEACON_TYPES.keys()))
        random_key = factory.make_string(prefix="_")
        random_value = factory.make_string()
        beacon = create_beacon_payload(
            random_type, payload={random_key: random_value}
        )
        # Ensure a valid UUID was added.
        self.assertIsNotNone(UUID(beacon.payload["uuid"]))
        self.assertThat(beacon.type, Equals(random_type))
        # The type is replicated here for authentication purposes.
        self.assertThat(
            beacon.payload["type"], Equals(BEACON_TYPES[random_type])
        )
        self.assertThat(beacon.payload[random_key], Equals(random_value))

    def test__creates_packet_that_can_decode(self):
        self.write_secret()
        random_type = random.choice(list(BEACON_TYPES.keys()))
        random_key = factory.make_string(prefix="_")
        random_value = factory.make_string()
        packet_bytes, _, _, _ = create_beacon_payload(
            random_type, payload={random_key: random_value}
        )
        decrypted = read_beacon_payload(packet_bytes)
        self.assertThat(decrypted.type, Equals(random_type))
        self.assertThat(decrypted.payload[random_key], Equals(random_value))


def _make_beacon_payload(version=1, type_code=1, length=None, payload=None):
    if payload is None:
        payload = b""
    if length is None:
        length = len(payload)
    packet = struct.pack(BEACON_HEADER_FORMAT_V1, version, type_code, length)
    return packet + payload


class TestReadBeaconPayload(SharedSecretTestCase):
    def test__raises_if_packet_too_small(self):
        with ExpectedException(
            InvalidBeaconingPacket, ".*packet must be at least 4 bytes.*"
        ):
            read_beacon_payload(b"")

    def test__raises_if_payload_too_small(self):
        packet = _make_beacon_payload(payload=b"1234")[:6]
        with ExpectedException(
            InvalidBeaconingPacket, ".*expected 4 bytes, got 2 bytes.*"
        ):
            read_beacon_payload(packet)

    def test__raises_when_version_incorrect(self):
        packet = _make_beacon_payload(version=0xFE)
        with ExpectedException(
            InvalidBeaconingPacket, ".*Unknown beacon version.*"
        ):
            read_beacon_payload(packet)

    def test__raises_when_inner_payload_does_not_decrypt(self):
        self.write_secret()
        packet = _make_beacon_payload(payload=b"\xfe")
        with ExpectedException(
            InvalidBeaconingPacket, ".*Failed to decrypt.*"
        ):
            read_beacon_payload(packet)

    def test__raises_when_inner_encapsulation_does_not_decompress(self):
        self.write_secret()
        packet = _make_beacon_payload(
            payload=fernet_encrypt_psk("\n\n", raw=True)
        )
        with ExpectedException(
            InvalidBeaconingPacket, ".*Failed to decompress.*"
        ):
            read_beacon_payload(packet)

    def test__raises_when_inner_encapsulation_is_not_bson(self):
        self.write_secret()
        payload = fernet_encrypt_psk(compress(b"\n\n"), raw=True)
        packet = _make_beacon_payload(payload=payload)
        with ExpectedException(
            InvalidBeaconingPacket, ".*beacon payload is not BSON.*"
        ):
            read_beacon_payload(packet)


class TestBeaconingPacket(MAASTestCase):
    def test__is_valid__succeeds_for_valid_payload(self):
        beacon = create_beacon_payload("solicitation")
        beacon_packet = BeaconingPacket(beacon.bytes)
        self.assertTrue(beacon_packet.valid)

    def test__is_valid__fails_for_invalid_payload(self):
        beacon = BeaconingPacket(b"\n\n\n\n")
        self.assertFalse(beacon.valid)


BEACON_PCAP = (
    b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00@\x00\x00\x01\x00\x00\x00v\xe19Y\xadF\x08\x00^\x00\x00\x00^\x00\x00"
    b"\x00\x01\x00^\x00\x00v\x00\x16>\x91zz\x08\x00E\x00\x00P\xe2E@\x00\x01"
    b"\x11\xe0\xce\xac\x10*\x02\xe0\x00\x00v\xda\xc2\x14x\x00<h(4\x00\x00\x00"
    b"\x02uuid\x00%\x00\x00\x0000000000-0000-0000-0000-000000000000\x00\x00"
)


class TestObserveBeaconsCommand(MAASTestCase):
    """Tests for `maas-rack observe-beacons`."""

    def test__requires_input_file(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args([])
        with ExpectedException(
            ActionScriptError, ".*Required argument: interface.*"
        ):
            run(args)

    def test__calls_subprocess_for_interface(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["eth0"])
        popen = self.patch(beaconing_module.subprocess, "Popen")
        popen.return_value.poll = Mock()
        popen.return_value.poll.return_value = None
        popen.return_value.stdout = io.BytesIO(BEACON_PCAP)
        output = io.StringIO()
        run(args, output=output)
        self.assertThat(
            popen,
            MockCalledOnceWith(
                ["sudo", "-n", "/usr/lib/maas/beacon-monitor", "eth0"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            ),
        )

    def test__calls_subprocess_for_interface_sudo(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["eth0"])
        popen = self.patch(beaconing_module.subprocess, "Popen")
        popen.return_value.poll = Mock()
        popen.return_value.poll.return_value = None
        popen.return_value.stdout = io.BytesIO(BEACON_PCAP)
        output = io.StringIO()
        run(args, output=output)
        self.assertThat(
            popen,
            MockCalledOnceWith(
                ["sudo", "-n", "/usr/lib/maas/beacon-monitor", "eth0"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
            ),
        )

    def test__checks_for_pipe(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["--input-file", "-"])
        output = io.StringIO()
        stdin = self.patch(beaconing_module.sys, "stdin")
        stdin.return_value.fileno = Mock()
        fstat = self.patch(beaconing_module.os, "fstat")
        fstat.return_value.st_mode = None
        stat = self.patch(beaconing_module.stat, "S_ISFIFO")
        stat.return_value = False
        with ExpectedException(
            ActionScriptError, "Expected stdin to be a pipe"
        ):
            run(args, output=output)

    def test__allows_pipe_input(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["--input-file", "-"])
        output = io.StringIO()
        stdin = self.patch(beaconing_module.sys, "stdin")
        stdin.return_value.fileno = Mock()
        fstat = self.patch(beaconing_module.os, "fstat")
        fstat.return_value.st_mode = None
        stat = self.patch(beaconing_module.stat, "S_ISFIFO")
        stat.return_value = True
        stdin_buffer = io.BytesIO(BEACON_PCAP)
        run(args, output=output, stdin_buffer=stdin_buffer)

    def test__allows_file_input(self):
        with NamedTemporaryFile("wb") as f:
            parser = ArgumentParser()
            add_arguments(parser)
            f.write(BEACON_PCAP)
            f.flush()
            args = parser.parse_args(["--input-file", f.name])
            output = io.StringIO()
            run(args, output=output)

    def test__raises_systemexit_observe_beaconing_return_code(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["eth0"])
        popen = self.patch(beaconing_module.subprocess, "Popen")
        popen.return_value.poll = Mock()
        popen.return_value.poll.return_value = None
        popen.return_value.stdout = io.BytesIO(BEACON_PCAP)
        output = io.StringIO()
        observe_beaconing_packets = self.patch(
            beaconing_module, "observe_beaconing_packets"
        )
        observe_beaconing_packets.return_value = 37
        with ExpectedException(SystemExit, ".*37.*"):
            run(args, output=output)

    def test__raises_systemexit_poll_result(self):
        parser = ArgumentParser()
        add_arguments(parser)
        args = parser.parse_args(["eth0"])
        popen = self.patch(beaconing_module.subprocess, "Popen")
        popen.return_value.poll = Mock()
        popen.return_value.poll.return_value = None
        popen.return_value.stdout = io.BytesIO(BEACON_PCAP)
        output = io.StringIO()
        observe_beaconing_packets = self.patch(
            beaconing_module, "observe_beaconing_packets"
        )
        observe_beaconing_packets.return_value = None
        popen.return_value.poll = Mock()
        popen.return_value.poll.return_value = 42
        with ExpectedException(SystemExit, ".*42.*"):
            run(args, output=output)

    def test__sets_self_as_process_group_leader(self):
        exception_type = factory.make_exception_type()
        os = self.patch(beaconing_module, "os")
        os.setpgrp.side_effect = exception_type
        self.assertRaises(exception_type, run, [])
        self.assertThat(os.setpgrp, MockCalledOnceWith())


class TestAgeOutUUIDQueue(MAASTestCase):
    """Tests for `age_out_uuid_queue()` function."""

    def test__does_not_remove_fresh_entries(self):
        uuid_now = str(uuid1())
        queue = OrderedDict()
        queue[uuid_now] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(1))

    def test__keeps_entries_from_the_reasonable_past(self):
        uuid_from_the_past = factory.make_UUID_with_timestamp(
            time.time() - 60.0
        )
        queue = OrderedDict()
        queue[uuid_from_the_past] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(1))

    def test__keeps_entries_from_the_reasonable_future(self):
        uuid_from_the_future = factory.make_UUID_with_timestamp(
            time.time() + 60.0
        )
        queue = OrderedDict()
        queue[uuid_from_the_future] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(1))

    def test__removes_entries_from_the_past(self):
        uuid_from_the_past = factory.make_UUID_with_timestamp(
            time.time() - 123.0
        )
        queue = OrderedDict()
        queue[uuid_from_the_past] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(0))

    def test__removes_entries_from_the_future(self):
        uuid_from_the_future = factory.make_UUID_with_timestamp(
            time.time() + 123.0
        )
        queue = OrderedDict()
        queue[uuid_from_the_future] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(0))

    def test__removes_entries_from_the_distant_past(self):
        uuid_from_the_past = "00000000-0000-1000-aaaa-aaaaaaaaaaaa"
        queue = OrderedDict()
        queue[uuid_from_the_past] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(0))

    def test__removes_entries_from_the_far_future(self):
        uuid_from_the_future = "ffffffff-ffff-1fff-0000-000000000000"
        queue = OrderedDict()
        queue[uuid_from_the_future] = {}
        self.assertThat(queue, HasLength(1))
        age_out_uuid_queue(queue)
        self.assertThat(queue, HasLength(0))
