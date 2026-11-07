import json
import socket
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from agent_workbench import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, value):
        response = Mock()
        response.read.return_value = value
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def test_existing_service_opens_without_starting_another(self):
        health = {"pid": 123, "data_dir": str(self.data)}
        with patch.object(launcher, "probe", return_value=health), patch.object(launcher, "spawn_server") as spawn:
            self.assertEqual(launcher.ensure_running(self.root, self.data), health)
        spawn.assert_not_called()

    def test_cold_start_waits_for_verified_health(self):
        health = {"pid": 123, "data_dir": str(self.data)}
        process = Mock()
        process.poll.return_value = None
        with patch.object(launcher, "probe", side_effect=[None, None, None, health]), patch.object(launcher, "spawn_server", return_value=process) as spawn:
            self.assertEqual(launcher.ensure_running(self.root, self.data), health)
        spawn.assert_called_once_with(self.root, self.data)

    def test_probe_checks_token_and_data_directory(self):
        opener = Mock()
        opener.open.side_effect = [self.response(b"const token='test-token'"),
            self.response(json.dumps({"data_dir": str(self.data)}).encode())]
        with patch.object(launcher, "build_opener", return_value=opener):
            self.assertIsNotNone(launcher.probe(self.data))
        self.assertEqual(opener.open.call_args[0][0].get_header("X-workbench-token"), "test-token")

    def test_unrelated_service_rejected_without_start(self):
        opener = Mock()
        opener.open.return_value = self.response(b"another application")
        with patch.object(launcher, "build_opener", return_value=opener), patch.object(launcher, "spawn_server") as spawn:
            with self.assertRaises(RuntimeError):
                launcher.ensure_running(self.root, self.data)
        spawn.assert_not_called()

    def test_another_workbench_directory_is_rejected(self):
        opener = Mock()
        opener.open.side_effect = [self.response(b"const token='test-token'"),
            self.response(json.dumps({"data_dir": str(self.root / "other")}).encode())]
        with patch.object(launcher, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError):
                launcher.probe(self.data)

    def test_refused_connection_is_offline_but_timeout_is_not(self):
        opener = Mock()
        with patch.object(launcher, "build_opener", return_value=opener):
            opener.open.side_effect = URLError(ConnectionRefusedError())
            self.assertIsNone(launcher.probe(self.data))
            opener.open.side_effect = URLError(TimeoutError())
            with self.assertRaises(RuntimeError):
                launcher.probe(self.data)

    def test_failed_child_reports_error(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(launcher, "probe", return_value=None), patch.object(launcher, "spawn_server", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "启动失败"):
                launcher.ensure_running(self.root, self.data)

    def test_closed_loopback_port_is_detected_as_offline(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.assertIsNone(launcher.probe(self.data, f"http://127.0.0.1:{port}/"))
