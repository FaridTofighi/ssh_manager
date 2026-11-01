"""Wake-on-LAN for an RDP host ("Wake and Connect"; off unless the host has a MAC address; pure Python).

A magic packet is 6 × ``FF`` followed by the MAC address 16 times, sent as
one UDP datagram to the configured broadcast address (port 9) — never to any
other address.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Callable, Optional

_MAC = re.compile(r"^([0-9A-Fa-f]{2})([:-]?)([0-9A-Fa-f]{2})(\2[0-9A-Fa-f]{2}){4}$")
WOL_PORT = 9


def parse_mac(text: str) -> Optional[bytes]:
    """``AA:BB:CC:DD:EE:FF`` / ``aa-bb-…`` / ``aabbccddeeff`` → 6 bytes, or ``None``."""

    value = (text or "").strip()
    if not _MAC.match(value):
        return None
    raw = bytes.fromhex(re.sub(r"[:-]", "", value))
    return raw if len(raw) == 6 and raw != b"\x00" * 6 and raw != b"\xff" * 6 else None


def magic_packet(mac: bytes) -> bytes:
    if len(mac) != 6:
        raise ValueError("a MAC address has 6 bytes")
    return b"\xff" * 6 + mac * 16


def broadcast_ok(address: str) -> bool:
    """An IPv4 address that may receive the packet (a broadcast or a private / link-local network address)."""

    try:
        ip = ipaddress.IPv4Address((address or "").strip())
    except ValueError:
        return False
    return ip == ipaddress.IPv4Address("255.255.255.255") or ip.is_private or ip.is_link_local


def wake(mac_text: str, broadcast: str = "255.255.255.255", *,
         sender: Optional[Callable[[bytes, str], None]] = None) -> None:
    """Send one magic packet (raises ``ValueError`` for a bad MAC or address, ``OSError`` from the socket)."""

    mac = parse_mac(mac_text)
    if mac is None:
        raise ValueError("The MAC address is not valid.")
    if not broadcast_ok(broadcast):
        raise ValueError("Wake-on-LAN packets go only to a broadcast or local network address.")
    packet = magic_packet(mac)
    if sender is not None:
        sender(packet, broadcast)
        return
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, WOL_PORT))


__all__ = ["parse_mac", "magic_packet", "broadcast_ok", "wake", "WOL_PORT"]
