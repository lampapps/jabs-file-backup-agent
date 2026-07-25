"""Shared utility helper functions for JABS."""

import socket


def get_primary_ipv4() -> str | None:
    """
    Get the primary local IPv4 address of the machine.

    Uses a socket connection to 8.8.8.8 to determine the outbound interface.
    This method doesn't actually send any packets, just determines which
    interface would be used for outbound connections.

    Returns:
        The primary IPv4 address as a string, or None if unable to determine.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            return ip
        finally:
            sock.close()
    except Exception:
        return None
