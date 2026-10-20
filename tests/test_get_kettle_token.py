"""Offline tests: a fake extractor module/connector stands in for Xiaomi Cloud.

Run with a Python that has `requests` (the venv from get-kettle-token.sh):
    ~/.cache/kettle-xiaomi/venv/bin/python -m unittest discover -s tests
"""

import contextlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

import requests
from requests.adapters import BaseAdapter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import get_kettle_token as gkt  # noqa: E402

BLE_TOKEN = "0123456789abcdef01234567"  # 24 hex: what Home Assistant wants
BEACONKEY = "0123456789abcdef0123456789abcdef"  # 32 hex: must NOT end up in KETTLE_BLE_TOKEN
KETTLE = {
    "did": "blt.3.1abcdefg",
    "mac": "AA:BB:CC:DD:EE:FF",
    "model": "yunmi.kettle.v14",
    "name": "Kettle",
    "token": BLE_TOKEN,
}
PASSWORD = "s3cret-Pa55 'quoted' $x"


class HangingAdapter(BaseAdapter):
    """Simulates a server that never answers: without a timeout, send() would block forever."""

    def __init__(self):
        super().__init__()
        self.timeouts = []

    def send(self, request, **kwargs):
        timeout = kwargs.get("timeout")
        self.timeouts.append(timeout)
        if timeout is None:
            raise AssertionError("request sent without timeout: it would hang forever")
        raise requests.exceptions.ReadTimeout("simulated hang")

    def close(self):
        pass


class FakeConnector:
    login_ok = True
    login_error = None
    devices_by_server = {}
    logins = []

    def __init__(self):
        self.userId = "1000"
        self._session = requests.Session()

    def login(self):
        type(self).logins.append((None,))
        if self.login_error:
            raise self.login_error
        return self.login_ok

    def get_homes(self, server):
        return {"result": {"homelist": [{"id": 1}]}}

    def get_dev_cnt(self, server):
        return {"result": {"share": {"share_family": []}}}

    def get_devices(self, server, home_id, owner_id):
        return {"result": {"device_info": self.devices_by_server.get(server, [])}}

    def get_beaconkey(self, server, did):
        return {"result": {"beaconkey": BEACONKEY}}


def fake_module(devices_by_server=None, **attrs):
    connector = type("Connector", (FakeConnector,), {
        "devices_by_server": devices_by_server if devices_by_server is not None else {"de": [KETTLE]},
        "logins": [],
        **attrs,
    })
    return types.SimpleNamespace(
        SERVERS=["cn", "de", "us"],
        args=types.SimpleNamespace(username=None, password=None),
        PasswordXiaomiCloudConnector=connector,
    )


def cli_args(region=None, mac=None):
    return types.SimpleNamespace(region=region, mac=mac)


def scripted(answers):
    queue = list(answers)
    return lambda prompt: queue.pop(0)


def no_prompt(prompt):
    raise AssertionError(f"unexpected prompt: {prompt}")


class ConfigDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = Path(self._tmp.name) / "cfg"
        patcher = unittest.mock.patch.object(gkt, "CONFIG_DIR", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_flow(self, mod, answers, region=None, mac=None, secret=lambda prompt: PASSWORD):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            gkt.run_cloud_flow(mod, cli_args(region, mac), scripted(answers), secret)
        return out.getvalue()

    def kettle_env(self):
        return (self.config / gkt.KETTLE_ENV_NAME).read_text().splitlines()


class FlowTests(ConfigDirCase):
    def test_ble_token_is_device_token_field_not_beaconkey(self):
        mod = fake_module()
        output = self.run_flow(mod, ["me@example.com", "de"])
        kettle = self.kettle_env()
        self.assertIn(f"KETTLE_BLE_TOKEN={BLE_TOKEN}", kettle)
        self.assertIn(f"KETTLE_BEACONKEY={BEACONKEY}", kettle)
        self.assertIn("KETTLE_MAC=AA:BB:CC:DD:EE:FF", kettle)
        self.assertIn("KETTLE_MODEL=yunmi.kettle.v14", kettle)
        self.assertFalse([line for line in kettle if line.startswith("KETTLE_TOKEN=")])
        self.assertIn(f"BLE TOKEN: {BLE_TOKEN}", output)
        self.assertEqual(stat.S_IMODE((self.config / gkt.KETTLE_ENV_NAME).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o700)

    def test_device_token_with_wrong_length_is_rejected(self):
        mod = fake_module({"de": [{**KETTLE, "token": BEACONKEY}]})
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de"])
        self.assertEqual(ctx.exception.code, gkt.EXIT_NOT_FOUND)
        self.assertFalse((self.config / gkt.KETTLE_ENV_NAME).exists())

    def test_missing_beaconkey_does_not_block_saving(self):
        mod = fake_module(get_beaconkey=lambda self, server, did: {"result": {}})
        self.run_flow(mod, ["me@example.com", "de"])
        kettle = self.kettle_env()
        self.assertIn(f"KETTLE_BLE_TOKEN={BLE_TOKEN}", kettle)
        self.assertFalse([line for line in kettle if line.startswith("KETTLE_BEACONKEY")])

    def test_first_run_saves_credentials_with_warning(self):
        output = self.run_flow(fake_module(), ["me@example.com", "de"])
        path = self.config / gkt.CREDENTIALS_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(gkt.load_credentials(), {
            "XIAOMI_USERNAME": "me@example.com",
            "XIAOMI_PASSWORD": PASSWORD,
            "XIAOMI_SERVER": "de",
        })
        self.assertIn("ОТКРЫТЫМ ТЕКСТОМ", output)
        self.assertIn("--forget", output)
        self.assertEqual(output.count("ОТКРЫТЫМ ТЕКСТОМ"), 1)

    def test_second_run_uses_saved_credentials_without_prompts_or_warning(self):
        self.run_flow(fake_module(), ["me@example.com", "de"])
        mod = fake_module()
        output = self.run_flow(mod, [], secret=no_prompt)
        self.assertNotIn("ОТКРЫТЫМ ТЕКСТОМ", output)
        self.assertIn("Использую сохранённые учётные данные", output)
        self.assertEqual(len(mod.PasswordXiaomiCloudConnector.logins), 1)

    def test_failed_login_with_saved_credentials_hints_forget_and_keeps_them(self):
        self.run_flow(fake_module(), ["me@example.com", "de"])
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(fake_module(login_ok=False), [], secret=no_prompt)
        self.assertEqual(ctx.exception.code, gkt.EXIT_LOGIN)
        self.assertIn("--forget", str(ctx.exception))
        self.assertTrue((self.config / gkt.CREDENTIALS_NAME).exists())

    def test_failed_login_does_not_save_credentials(self):
        mod = fake_module(login_ok=False)
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de"])
        self.assertEqual(ctx.exception.code, gkt.EXIT_LOGIN)
        self.assertIsNone(mod.args.password)
        self.assertFalse(self.config.exists())

    def test_login_exception_message_is_not_leaked(self):
        mod = fake_module(login_error=RuntimeError("url?hash=SECRETHASH"))
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de"])
        self.assertNotIn("SECRETHASH", str(ctx.exception))

    def test_password_never_printed(self):
        output = self.run_flow(fake_module(), ["me@example.com", "de"])
        self.assertNotIn(PASSWORD, output)
        self.assertNotIn(PASSWORD, (self.config / gkt.KETTLE_ENV_NAME).read_text())

    def test_other_region_found_updates_saved_server(self):
        mod = fake_module({"us": [KETTLE]})
        self.run_flow(mod, ["me@example.com", "de", "y"])
        creds = gkt.load_credentials()
        self.assertEqual(creds["XIAOMI_SERVER"], "us")
        self.assertEqual(creds["XIAOMI_PASSWORD"], PASSWORD)

    def test_not_found_does_not_write_kettle_env(self):
        mod = fake_module({})
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de", "n"])
        self.assertEqual(ctx.exception.code, gkt.EXIT_NOT_FOUND)
        self.assertFalse((self.config / gkt.KETTLE_ENV_NAME).exists())

    def test_kettle_picked_by_model_when_mac_differs(self):
        mod = fake_module({"de": [{**KETTLE, "mac": "11:22:33:44:55:66"}]})
        output = self.run_flow(mod, ["me@example.com", "de"])
        self.assertIn("MAC:       11:22:33:44:55:66", output)

    def test_progress_line_per_region(self):
        output = self.run_flow(fake_module(), ["me@example.com", "de"])
        self.assertIn("de: homes=1, devices=1", output)


class ForgetTests(ConfigDirCase):
    def test_forget_removes_credentials_but_keeps_kettle_env(self):
        self.run_flow(fake_module(), ["me@example.com", "de"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gkt.main(["--forget"]), gkt.EXIT_OK)
        self.assertFalse((self.config / gkt.CREDENTIALS_NAME).exists())
        self.assertTrue((self.config / gkt.KETTLE_ENV_NAME).exists())

    def test_forget_without_file_is_ok(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(gkt.main(["--forget"]), gkt.EXIT_OK)
        self.assertIn("нет", out.getvalue())


class TimeoutTests(ConfigDirCase):
    def test_default_timeout_is_applied_to_session_requests(self):
        session = requests.Session()
        adapter = HangingAdapter()
        session.mount("https://", adapter)
        gkt.apply_default_timeout(session)
        with self.assertRaises(requests.exceptions.ReadTimeout):
            session.post("https://example.invalid/x", params={"a": "b"})
        with self.assertRaises(requests.exceptions.ReadTimeout):
            session.get("https://example.invalid/x")
        self.assertEqual(adapter.timeouts, [gkt.REQUEST_TIMEOUT, gkt.REQUEST_TIMEOUT])

    def test_explicit_timeout_is_kept(self):
        session = requests.Session()
        adapter = HangingAdapter()
        session.mount("https://", adapter)
        gkt.apply_default_timeout(session)
        with self.assertRaises(requests.exceptions.ReadTimeout):
            session.get("https://example.invalid/x", timeout=3)
        self.assertEqual(adapter.timeouts, [3])

    def test_hanging_login_fails_fast_with_login_exit_code(self):
        adapter = HangingAdapter()

        def hanging_login(self):
            self._session.mount("https://", adapter)
            self._session.post("https://account.xiaomi.com/pass/serviceLoginAuth2")

        mod = fake_module(login=hanging_login)
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de"])
        self.assertEqual(ctx.exception.code, gkt.EXIT_LOGIN)
        self.assertIn("ReadTimeout", str(ctx.exception))
        self.assertEqual(adapter.timeouts, [gkt.REQUEST_TIMEOUT])
        self.assertFalse((self.config / gkt.CREDENTIALS_NAME).exists())

    def test_hanging_cloud_call_after_login_fails_fast(self):
        adapter = HangingAdapter()

        def hanging_homes(self, server):
            self._session.mount("https://", adapter)
            return self._session.post("https://de.api.io.mi.com/app/v2/homeroom/gethome")

        mod = fake_module(get_homes=hanging_homes)
        with self.assertRaises(gkt.Fatal) as ctx:
            self.run_flow(mod, ["me@example.com", "de"])
        self.assertEqual(ctx.exception.code, gkt.EXIT_LOGIN)
        self.assertEqual(adapter.timeouts, [gkt.REQUEST_TIMEOUT])
        # login succeeded, so credentials are kept for the retry
        self.assertTrue((self.config / gkt.CREDENTIALS_NAME).exists())


class HelperTests(unittest.TestCase):
    def test_normalize_mac(self):
        self.assertEqual(gkt.normalize_mac("aa-bb-cc-dd-ee-ff"), "AABBCCDDEEFF")
        self.assertEqual(gkt.normalize_mac("nonsense"), "")

    def test_find_candidates_prefers_mac_match(self):
        conn = FakeConnector()
        conn.devices_by_server = {"de": [{**KETTLE, "mac": "00:00:00:00:00:01"}, {"did": "x", "mac": "AA:BB:CC:DD:EE:FF", "model": "other"}]}
        with contextlib.redirect_stdout(io.StringIO()):
            found = gkt.find_candidates(conn, ["de"], "AABBCCDDEEFF")
        self.assertEqual([d["model"] for _, d in found], ["other"])


class ShowHaSetupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def run_script(self, env_text):
        cfg = self.home / ".config" / "kettle-xiaomi"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "kettle.env").write_text(env_text)
        return subprocess.run(
            [str(REPO / "scripts" / "show-ha-setup.sh")], env={**os.environ, "HOME": str(self.home)}, capture_output=True, text=True
        )

    def test_prints_ble_token_not_beaconkey(self):
        result = self.run_script(
            f"KETTLE_MAC=AA:BB:CC:DD:EE:FF\nKETTLE_MODEL=yunmi.kettle.v14\nKETTLE_BLE_TOKEN={BLE_TOKEN}\nKETTLE_BEACONKEY={BEACONKEY}\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("AA:BB:CC:DD:EE:FF", result.stdout)
        self.assertIn(f"Токен: {BLE_TOKEN}", result.stdout)
        self.assertNotIn(BEACONKEY, result.stdout)

    def test_wrong_token_length_is_a_clear_error(self):
        result = self.run_script(
            f"KETTLE_MAC=AA:BB:CC:DD:EE:FF\nKETTLE_MODEL=yunmi.kettle.v14\nKETTLE_BLE_TOKEN={BEACONKEY}\n"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("24 hex", result.stderr)
        self.assertNotIn(BEACONKEY, result.stdout)

    def test_old_format_file_asks_to_rerun(self):
        result = self.run_script(
            f"KETTLE_MAC=AA:BB:CC:DD:EE:FF\nKETTLE_MODEL=yunmi.kettle.v14\nKETTLE_TOKEN={BEACONKEY}\n"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("KETTLE_BLE_TOKEN", result.stderr)

    def test_env_file_is_never_executed(self):
        marker = self.home / "pwned"
        result = self.run_script(
            f"KETTLE_MAC=AA:BB:CC:DD:EE:FF\nKETTLE_MODEL=yunmi.kettle.v14\nKETTLE_BLE_TOKEN={BLE_TOKEN}\n$(touch {marker})\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
