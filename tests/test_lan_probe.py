"""The LAN probe: one command at a time, nothing secret in its answers."""

import struct

import pytest

from custom_components.ezviz_instant_bell.cpd7 import lan_client as lan
from custom_components.ezviz_instant_bell.cpd7 import lan_probe

HOST = "192.168.1.20"
KEY = b"0123456789abcdef"


def answer(cmd: int, body: bytes) -> bytes:
    return lan._build_packet(1, cmd, body, KEY)


def plain_answer(cmd: int) -> bytes:
    body = b'<?xml version="1.0" encoding="utf-8"?>\n<Response><Result>3</Result></Response>'
    header = bytearray(lan.HEADER_LEN)
    header[:4] = lan.MAGIC
    header[16:20] = struct.pack(">I", cmd)
    header[24:28] = struct.pack(">I", len(body))
    return bytes(header) + body + b"0" * 32


def sent_command(packet: bytes) -> int:
    return struct.unpack(">I", packet[16:20])[0]


def test_values_of_secret_looking_fields_are_hidden():
    text = '<Session Key="abcdefghijklmnop" OperationCode="XYZ" Id="7"/><DevPassword>pw</DevPassword><Name>Door</Name>'
    hidden = lan_probe.redact(text)
    assert "abcdefghijklmnop" not in hidden and "XYZ" not in hidden and ">pw<" not in hidden
    assert 'Id="7"' in hidden and "<Name>Door</Name>" in hidden


@pytest.mark.parametrize(("text", "first", "count"), [("2000-20ff", 0x2000, 256), ("2013", 0x2013, 1)])
def test_ranges(text, first, count):
    commands = lan_probe.parse_range(text)
    assert commands[0] == first and len(commands) == count


@pytest.mark.parametrize("text", ["2000-2400", "20ff-2000", "zz", "10000"])
def test_bad_or_too_large_ranges_are_refused(text):
    with pytest.raises(ValueError):
        lan_probe.parse_range(text)


def test_discover_keeps_the_commands_that_answer_with_the_next_number():
    sent, pauses = [], []

    def exchange(host, packet, _timeout):
        cmd = sent_command(packet)
        sent.append(cmd)
        return plain_answer(cmd + 1) if cmd in (0x2007, 0x2011) else None

    found = lan_probe.discover(HOST, range(0x2005, 0x2013), exchange=exchange, sleep=pauses.append)
    assert found == [0x2007, 0x2011]
    assert sent == list(range(0x2005, 0x2013))
    assert len(pauses) == len(sent)


def test_discover_never_sends_a_readable_request():
    packets = []
    lan_probe.discover(HOST, [0x2007], exchange=lambda h, p, t: packets.append(p), sleep=lambda _s: None)
    body = packets[0][lan.HEADER_LEN : lan.HEADER_LEN + 32]
    assert body == b"\x00" * 32


def test_discover_stops_when_the_device_refuses_connections():
    def exchange(host, packet, _timeout):
        raise ConnectionRefusedError("refused")

    with pytest.raises(ConnectionError, match="0x2000"):
        lan_probe.discover(HOST, [0x2000, 0x2001], exchange=exchange, sleep=lambda _s: None)


def test_discover_refuses_hosts_outside_the_local_network():
    with pytest.raises(ConnectionError):
        lan_probe.discover("8.8.8.8", [0x2000], exchange=lambda h, p, t: None, sleep=lambda _s: None)


def test_ask_decrypts_and_hides_the_answers():
    def exchange(host, packet, _timeout):
        cmd = sent_command(packet)
        if cmd == 0x2007:
            return answer(0x2008, b'<Response><Result>0</Result><Session Key="SECRETSECRET1234"/></Response>')
        return None

    answers = lan_probe.ask(HOST, KEY, [0x2007, 0x2009], exchange=exchange, sleep=lambda _s: None)
    assert answers["0x2007"].startswith("answer 0x2008:")
    assert "SECRETSECRET1234" not in answers["0x2007"] and "<Result>0</Result>" in answers["0x2007"]
    assert answers["0x2009"] == "closed without an answer"


def test_ask_sends_an_empty_request():
    packets = []

    def exchange(host, packet, _timeout):
        packets.append(packet)

    lan_probe.ask(HOST, KEY, [0x2023], exchange=exchange, sleep=lambda _s: None)
    meta, plain = lan._parse_response(packets[0], KEY)
    assert meta["cmd"] == 0x2023
    assert plain == lan_probe._empty_request()
    assert b"<OperationCode>ABCDEFG</OperationCode>" in plain and plain.count(b"</") == 2


def test_ask_stops_at_the_first_refused_connection():
    calls = []

    def exchange(host, packet, _timeout):
        calls.append(packet)
        raise ConnectionRefusedError("refused")

    answers = lan_probe.ask(HOST, KEY, [0x2007, 0x2009], exchange=exchange, sleep=lambda _s: None)
    assert len(calls) == 1 and list(answers) == ["0x2007"]
