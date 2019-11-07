# Copyright 2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for ``provisioningserver.utils.pcap``."""

__all__ = []

import io

from maastesting.testcase import MAASTestCase
from provisioningserver.utils.pcap import PCAP, PCAPError
from testtools import ExpectedException
from testtools.matchers import Equals

# Created with:
# $ sudo tcpdump -i eth0 -U --immediate-mode -s 64 -n -c 2 -w - arp \
#     | utilities/bin2python.py
TESTDATA = (
    b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"@\x00\x00\x00\x01\x00\x00\x00\x1a\x8aqW\xce6\x0e\x00<\x00\x00\x00"
    b"<\x00\x00\x00\xff\xff\xff\xff\xff\xff\x00$\xa5\xaf$\x85\x08\x06\x00\x01"
    b"\x08\x00\x06\x04\x00\x01\x00$\xa5\xaf$\x85\xac\x10*\x01\x00\x00\x00\x00"
    b"\x00\x00\xac\x10*\xa7\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x00\x00\x00\x1b\x8aqW\xcb\xce\x05\x00<\x00\x00\x00"
    b"<\x00\x00\x00\x80\xfa[\x0cFN\x00$\xa5\xaf$\x85\x08\x06\x00\x01"
    b"\x08\x00\x06\x04\x00\x01\x00$\xa5\xaf$\x85\xac\x10*\x01\x00\x00\x00\x00"
    b"\x00\x00\xac\x10*m\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"\x00\x00\x00\x00\x00"
)

TESTDATA_INVALID_PACKET_HEADER = (
    b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"@\x00\x00\x00\x01\x00\x00\x00\x1a"
)

TESTDATA_INVALID_PACKET = (
    b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    b"@\x00\x00\x00\x01\x00\x00\x00\x1a\x8aqW\xce6\x0e\x00<\x00\x00\x00"
    b"<\x00\x00\x00\xff\xff\xff\xff\xff\xff\x00$"
    b"\x00\x00\x00\x00\x00"
)


class TestPCAP(MAASTestCase):
    def test__raises_EOFError_for_empty_PCAP_stream(self):
        stream = io.BytesIO(b"")
        with ExpectedException(EOFError, "No PCAP output found."):
            PCAP(stream)

    def test__raises_PCAPError_for_invalid_PCAP_stream(self):
        stream = io.BytesIO(b"\0" * 24)
        with ExpectedException(
            PCAPError, "Stream is not in native PCAP format."
        ):
            PCAP(stream)

    def test__raises_PCAPError_for_invalid_PCAP_header(self):
        stream = io.BytesIO(b"\0" * 5)
        with ExpectedException(
            PCAPError, "Unexpected end of PCAP stream: invalid header."
        ):
            PCAP(stream)

    def test__parses_valid_stream(self):
        stream = io.BytesIO(TESTDATA)
        pcap = PCAP(stream)
        header = pcap.global_header
        self.assertThat(header, Equals((2712847316, 2, 4, 0, 0, 64, 1)))
        pkt1 = pcap.read()
        self.assertThat(pkt1[0], Equals((1467058714, 931534, 60, 60)))
        self.assertThat(
            pkt1[1],
            Equals(
                b"\xff\xff\xff\xff\xff\xff\x00$\xa5\xaf$\x85\x08\x06\x00\x01\x08"
                b"\x00\x06\x04\x00\x01\x00$\xa5\xaf$\x85\xac\x10*\x01\x00\x00\x00"
                b"\x00\x00\x00\xac\x10*\xa7\x00\x00\x00\x00\x00\x00\x00\x00\x00"
                b"\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            ),
        )
        pkt2 = pcap.read()
        self.assertThat(pkt2[0], Equals((1467058715, 380619, 60, 60)))
        self.assertThat(
            pkt2[1],
            Equals(
                b"\x80\xfa[\x0cFN\x00$\xa5\xaf$\x85\x08\x06\x00\x01\x08\x00\x06"
                b"\x04\x00\x01\x00$\xa5\xaf$\x85\xac\x10*\x01\x00\x00\x00\x00\x00"
                b"\x00\xac\x10*m\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
                b"\x00\x00\x00\x00\x00\x00"
            ),
        )

    def test__raises_EOFError_for_end_of_stream(self):
        stream = io.BytesIO(TESTDATA)
        pcap = PCAP(stream)
        pcap.read()
        pcap.read()
        with ExpectedException(EOFError, "End of PCAP stream."):
            pcap.read()

    def test__iterator(self):
        stream = io.BytesIO(TESTDATA)
        pcap = PCAP(stream)
        count = 0
        for _ in pcap:
            count += 1
        # Expect no exception to have been thrown, and there are two packets.
        self.assertThat(count, Equals(2))

    def test__raises_PCAPError_for_invalid_packet_header(self):
        stream = io.BytesIO(TESTDATA_INVALID_PACKET_HEADER)
        pcap = PCAP(stream)
        header = pcap.global_header
        self.assertThat(header, Equals((2712847316, 2, 4, 0, 0, 64, 1)))
        with ExpectedException(
            PCAPError, "Unexpected end of PCAP stream: invalid packet header."
        ):
            pcap.read()

    def test__raises_PCAPError_for_invalid_packet(self):
        stream = io.BytesIO(TESTDATA_INVALID_PACKET)
        pcap = PCAP(stream)
        header = pcap.global_header
        self.assertThat(header, Equals((2712847316, 2, 4, 0, 0, 64, 1)))
        with ExpectedException(
            PCAPError, "Unexpected end of PCAP stream: invalid packet."
        ):
            pcap.read()
