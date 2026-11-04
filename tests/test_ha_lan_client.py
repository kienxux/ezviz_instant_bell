"""What the LAN client accepts from the door station."""

import hashlib
import struct

import pytest

from custom_components.ezviz_instant_bell.cpd7 import lan_client as lan


class FakeSocket:
    def __init__(self, data: bytes, step: int = 7):
        self._data, self._step = data, step

    def settimeout(self, _t):
        pass

    def recv(self, count):
        chunk, self._data = self._data[: min(count, self._step)], self._data[min(count, self._step):]
        return chunk


def wire(body: bytes, trailer: bytes | None = None, declared: int | None = None) -> bytes:
    header = bytearray(lan.HEADER_LEN)
    header[:4] = lan.MAGIC
    header[24:28] = struct.pack(">I", len(body) if declared is None else declared)
    return bytes(header) + body + (hashlib.md5(body).hexdigest().encode() if trailer is None else trailer)


def test_a_normal_answer_is_read_in_pieces():
    data = wire(b"x" * 300)
    assert lan._recv_response(FakeSocket(data)) == data


def test_an_absurd_length_is_refused_before_reading_it():
    with pytest.raises(ConnectionError, match="too long"):
        lan._recv_response(FakeSocket(wire(b"", declared=4_000_000_000)))


def test_a_cut_off_answer_is_an_error():
    with pytest.raises(ConnectionError):
        lan._recv_response(FakeSocket(wire(b"x" * 300)[:-5]))


def test_a_wrong_trailer_is_noted_but_not_fatal():
    data = wire(b"x" * 10, trailer=b"0" * 32)
    assert lan._recv_response(FakeSocket(data)) == data


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5", "172.16.3.4", "169.254.1.1", "100.64.0.9"])
def test_local_hosts_are_accepted(host):
    lan.check_lan_host(host)


@pytest.mark.parametrize("host", ["8.8.8.8", "93.184.216.34", "0.0.0.0", "224.0.0.1", "example.com", ""])
def test_other_hosts_are_refused(host):
    with pytest.raises(ConnectionError):
        lan.check_lan_host(host)
