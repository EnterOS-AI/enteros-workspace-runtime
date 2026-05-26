"""Tests for get_machine_ip env-var configurability (RCA #52 Finding 4)."""
import socket
from unittest.mock import MagicMock, patch

import pytest

from molecule_runtime.main import get_machine_ip


class TestGetMachineIp:
    def test_uses_default_probe_when_no_env_set(self, monkeypatch):
        monkeypatch.delenv("MOLECULE_NETWORK_PROBE_HOST", raising=False)
        monkeypatch.delenv("MOLECULE_NETWORK_PROBE_PORT", raising=False)

        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("192.168.1.5", 54321)

        with patch("socket.socket", return_value=mock_sock) as mock_ctor:
            ip = get_machine_ip()

        assert ip == "192.168.1.5"
        mock_ctor.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
        mock_sock.connect.assert_called_once_with(("8.8.8.8", 80))
        mock_sock.close.assert_called_once()

    def test_uses_custom_probe_host_and_port(self, monkeypatch):
        monkeypatch.setenv("MOLECULE_NETWORK_PROBE_HOST", "10.0.0.1")
        monkeypatch.setenv("MOLECULE_NETWORK_PROBE_PORT", "9999")

        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("10.0.0.5", 12345)

        with patch("socket.socket", return_value=mock_sock):
            ip = get_machine_ip()

        assert ip == "10.0.0.5"
        mock_sock.connect.assert_called_once_with(("10.0.0.1", 9999))

    def test_invalid_port_env_falls_back_to_80(self, monkeypatch):
        monkeypatch.setenv("MOLECULE_NETWORK_PROBE_PORT", "not-a-number")

        mock_sock = MagicMock()
        mock_sock.getsockname.return_value = ("172.16.0.2", 33333)

        with patch("socket.socket", return_value=mock_sock):
            ip = get_machine_ip()

        assert ip == "172.16.0.2"
        mock_sock.connect.assert_called_once_with(("8.8.8.8", 80))

    def test_socket_error_returns_loopback(self, monkeypatch):
        monkeypatch.delenv("MOLECULE_NETWORK_PROBE_HOST", raising=False)
        monkeypatch.delenv("MOLECULE_NETWORK_PROBE_PORT", raising=False)

        with patch("socket.socket", side_effect=OSError("no route")):
            ip = get_machine_ip()

        assert ip == "127.0.0.1"
