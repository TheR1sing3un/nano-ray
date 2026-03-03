"""TCP transport layer for nano-ray multi-node communication.

Provides a simple length-prefixed message protocol over TCP.
Each message is serialized with cloudpickle, prefixed with a 4-byte
big-endian length header.

Wire format:
    [4 bytes: message length N (big-endian)] [N bytes: cloudpickle payload]

This is a simplified version of Ray's gRPC-based transport. Ray uses
protobuf + gRPC for control plane and custom direct-transfer for data
plane. nano-ray uses a single TCP channel for both, which is simpler
but less efficient for large objects.

SIMPLIFICATION: Ray uses gRPC with protobuf for structured messages.
We use raw TCP + cloudpickle for simplicity, trading type safety and
interoperability for ease of implementation.
"""

from __future__ import annotations

import socket
import struct
from typing import Any

import cloudpickle


# 4 bytes, big-endian unsigned int
_HEADER_FMT = "!I"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_MAX_MESSAGE_SIZE = 256 * 1024 * 1024  # 256 MB


def send_msg(sock: socket.socket, obj: Any) -> None:
    """Send a Python object over a TCP socket.

    Serializes with cloudpickle and sends with a length prefix.
    """
    data = cloudpickle.dumps(obj)
    if len(data) > _MAX_MESSAGE_SIZE:
        raise ValueError(
            f"Message too large: {len(data)} bytes (max {_MAX_MESSAGE_SIZE})"
        )
    header = struct.pack(_HEADER_FMT, len(data))
    sock.sendall(header + data)


def recv_msg(sock: socket.socket) -> Any:
    """Receive a Python object from a TCP socket.

    Reads the length prefix, then reads the full payload and deserializes.
    Returns None if the connection is closed.
    """
    header = _recv_exact(sock, _HEADER_SIZE)
    if header is None:
        return None
    (length,) = struct.unpack(_HEADER_FMT, header)
    if length > _MAX_MESSAGE_SIZE:
        raise ValueError(
            f"Message too large: {length} bytes (max {_MAX_MESSAGE_SIZE})"
        )
    data = _recv_exact(sock, length)
    if data is None:
        return None
    return cloudpickle.loads(data)


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """Read exactly n bytes from a socket. Returns None on EOF."""
    chunks: list[bytes] = []
    received = 0
    while received < n:
        chunk = sock.recv(n - received)
        if not chunk:
            return None
        chunks.append(chunk)
        received += len(chunk)
    return b"".join(chunks)
