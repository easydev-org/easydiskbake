"""The arrival check must not be satisfiable by qemu's port forward.

This is the test for the bug that produced a published empty disk: `-netdev
user,hostfwd=tcp::3389-:3389` binds the host port when qemu starts, so a bare TCP
connect succeeds before anything has booted. A bake using port_open() declared the
guest "a running Windows" six seconds after qemu-img create.

rdp_serving() reads the server's first bytes instead. RDP is X.224 over TPKT: the
server speaks first, and the header is version 3 with a length of at least 11.

These tests run a fake server rather than qemu, which is the point - the protocol
check is the thing that has to be right, and it can be wrong independently of any
guest.
"""

import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import vm  # noqa: E402


class FakeServer:
    """Accepts one connection and sends `payload`, or sends nothing at all."""

    def __init__(self, payload: bytes):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.payload = payload
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
            with conn:
                if self.payload:
                    conn.sendall(self.payload)
                # Hold the connection open long enough for the client's recv to
                # either get the bytes or time out, which is what is being tested.
                conn.settimeout(3)
                try:
                    conn.recv(1)
                except OSError:
                    pass
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# A real Connection Confirm as Windows sends it: TPKT header (03 00) then a length
# that covers the 11-byte minimum.
RDP_CONFIRM = bytes([0x03, 0x00, 0x00, 0x13, 0x0e, 0xd0, 0x00, 0x00, 0x12, 0x34, 0x00])


class RdpProtocol(unittest.TestCase):

    def test_real_connection_confirm_is_accepted(self):
        server = FakeServer(RDP_CONFIRM)
        try:
            self.assertTrue(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_silent_forward_is_rejected(self):
        # This is exactly what qemu's hostfwd does before the guest boots: it
        # accepts and then says nothing. The old port_open() check called this a
        # running Windows.
        server = FakeServer(b"")
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_non_rdp_service_is_rejected(self):
        # An SSH banner, or anything else that happens to be on the port.
        server = FakeServer(b"SSH-2.0-OpenSSH_9.6\r\n")
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_http_response_is_rejected(self):
        server = FakeServer(b"HTTP/1.1 400 Bad Request\r\n")
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_closed_port_is_rejected(self):
        # Nothing listening at all.
        self.assertFalse(vm.rdp_serving(1))

    def test_version_byte_must_be_three(self):
        # Correct length, wrong TPKT version.
        server = FakeServer(bytes([0x02, 0x00, 0x00, 0x13, 0x00]))
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_length_must_be_at_least_eleven(self):
        # Version and reserved look right but the length is too short for an X.224
        # TPDU, which is what a truncated or unrelated response looks like.
        server = FakeServer(bytes([0x03, 0x00, 0x00, 0x04, 0x00]))
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()

    def test_short_read_is_rejected(self):
        # Fewer than four bytes cannot carry a TPKT header.
        server = FakeServer(bytes([0x03, 0x00]))
        try:
            self.assertFalse(vm.rdp_serving(server.port))
        finally:
            server.close()


class PortOpenIsStillJustTcp(unittest.TestCase):
    """port_open is kept and kept honest: it answers a narrower question."""

    def test_port_open_succeeds_on_a_silent_listener(self):
        # A silent socket is open. That is all port_open claims, and the reason it
        # must not be used for arrival.
        server = FakeServer(b"")
        try:
            self.assertTrue(vm.port_open("127.0.0.1", server.port))
        finally:
            server.close()

    def test_port_open_fails_when_closed(self):
        self.assertFalse(vm.port_open("127.0.0.1", 1))


if __name__ == "__main__":
    unittest.main()
