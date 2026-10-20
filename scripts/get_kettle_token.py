#!/usr/bin/env python3
"""Fetch MAC, model and BLE token of a Yunmi kettle from Xiaomi Cloud.

Normally started through get-kettle-token.sh, which prepares a venv and a pinned copy
of Xiaomi-cloud-tokens-extractor and passes it via --extractor.

Credentials are typed interactively (password via getpass) on the first run and, after
a successful login, saved in plain text to ~/.config/kettle-xiaomi/credentials (dir 700,
file 600) so later runs do not ask again. --forget deletes that file. The password is
never passed through argv, the environment or logs.

Exit codes: 0 ok, 1 unexpected error, 2 usage / no interactive terminal,
3 extractor or dependencies unusable, 4 login or cloud request failed,
5 kettle not found, 130 interrupted.
"""

import argparse
import getpass
import importlib.util
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

DEFAULT_SERVER = "de"
ALL_SERVERS = "all"
MODEL_PREFIX = "yunmi.kettle."
# (connect, read) seconds: the extractor calls session.post/get without any timeout.
REQUEST_TIMEOUT = (10, 30)

CONFIG_DIR = Path.home() / ".config" / "kettle-xiaomi"
KETTLE_ENV_NAME = "kettle.env"
CREDENTIALS_NAME = "credentials"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_SETUP = 3
EXIT_LOGIN = 4
EXIT_NOT_FOUND = 5
EXIT_INTERRUPTED = 130

SAFE_IDENT = re.compile(r"[A-Za-z0-9._-]{1,64}")
SAFE_KEY = re.compile(r"[A-Za-z0-9+/=_-]{8,128}")
# Home Assistant's Yunmi Kettle integration takes the device's `token` field: 12 bytes = 24 hex.
BLE_TOKEN = re.compile(r"[0-9a-fA-F]{24}")


class Fatal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def normalize_mac(value):
    digits = re.sub(r"[^0-9A-Fa-f]", "", value or "").upper()
    return digits if len(digits) == 12 else ""


def format_mac(digits):
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def load_extractor(path):
    """Import the upstream script as a module. It parses sys.argv at import time."""
    saved_argv = sys.argv
    sys.argv = [str(path)]
    try:
        spec = importlib.util.spec_from_file_location("token_extractor", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["token_extractor"] = module
        spec.loader.exec_module(module)
    except (ImportError, OSError, SystemExit) as exc:
        raise Fatal(EXIT_SETUP, f"не удалось импортировать extractor ({type(exc).__name__}): {exc}") from exc
    finally:
        sys.argv = saved_argv
    for name in ("PasswordXiaomiCloudConnector", "SERVERS", "args"):
        if not hasattr(module, name):
            raise Fatal(EXIT_SETUP, f"в extractor нет ожидаемого '{name}' — версия не совпадает с закреплённой")
    return module


def apply_default_timeout(session, timeout=REQUEST_TIMEOUT):
    """Make every request of a requests.Session carry a timeout unless the caller set one."""
    original = session.request

    def request(method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = timeout
        return original(method, url, **kwargs)

    session.request = request


def ensure_config_dir():
    CONFIG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)


def write_private(name, lines):
    """Atomically write CONFIG_DIR/name with mode 600."""
    ensure_config_dir()
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("".join(line + "\n" for line in lines))
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CONFIG_DIR / name)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


CREDENTIAL_KEYS = ("XIAOMI_USERNAME", "XIAOMI_PASSWORD", "XIAOMI_SERVER")


def load_credentials():
    try:
        text = (CONFIG_DIR / CREDENTIALS_NAME).read_text()
    except OSError:
        return {}
    credentials = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in CREDENTIAL_KEYS:
            continue
        try:
            parts = shlex.split(value)
        except ValueError:
            continue
        if len(parts) == 1:
            credentials[key] = parts[0]
    return credentials


def save_credentials(username, password, server):
    write_private(CREDENTIALS_NAME, [
        f"XIAOMI_USERNAME={shlex.quote(username)}",
        f"XIAOMI_PASSWORD={shlex.quote(password)}",
        f"XIAOMI_SERVER={shlex.quote(server)}",
    ])


def forget_credentials():
    path = CONFIG_DIR / CREDENTIALS_NAME
    try:
        path.unlink()
    except FileNotFoundError:
        print(f"Сохранённых учётных данных нет ({path}).")
        return
    print(f"Удалено: {path}. Файл {KETTLE_ENV_NAME} (токен чайника) не тронут.")


def save_kettle(mac, model, ble_token, beaconkey, did):
    if not normalize_mac(mac):
        raise Fatal(EXIT_ERROR, f"неожиданный формат MAC: {mac!r}")
    if not SAFE_IDENT.fullmatch(model):
        raise Fatal(EXIT_ERROR, f"неожиданный формат model: {model!r}")
    if not BLE_TOKEN.fullmatch(ble_token):
        raise Fatal(EXIT_ERROR, "поле token устройства не 24 hex-символа, не сохраняю")
    if beaconkey and not SAFE_KEY.fullmatch(beaconkey):
        raise Fatal(EXIT_ERROR, "beaconkey имеет неожиданный формат, не сохраняю")
    if not SAFE_IDENT.fullmatch(did):
        raise Fatal(EXIT_ERROR, f"неожиданный формат did: {did!r}")
    lines = [
        f"KETTLE_MAC={format_mac(normalize_mac(mac))}",
        f"KETTLE_MODEL={model}",
        f"KETTLE_DID={did}",
        f"KETTLE_BLE_TOKEN={ble_token.lower()}",
    ]
    if beaconkey:
        lines.append(f"KETTLE_BEACONKEY={beaconkey}")
    write_private(KETTLE_ENV_NAME, lines)
    return CONFIG_DIR / KETTLE_ENV_NAME


def collect_devices(conn, server):
    """Return every device visible to the account on one server (own and shared homes)."""
    homes = []
    homes_resp = conn.get_homes(server)
    for home in ((homes_resp or {}).get("result") or {}).get("homelist") or []:
        homes.append((home["id"], conn.userId))
    cnt_resp = conn.get_dev_cnt(server)
    share = ((cnt_resp or {}).get("result") or {}).get("share") or {}
    for family in share.get("share_family") or []:
        homes.append((family["home_id"], family["home_owner"]))
    devices = []
    for home_id, owner_id in homes:
        devices_resp = conn.get_devices(server, home_id, owner_id)
        devices.extend(((devices_resp or {}).get("result") or {}).get("device_info") or [])
    print(f"{server}: homes={len(homes)}, devices={len(devices)}", flush=True)
    return devices


def find_candidates(conn, servers, mac_digits):
    """Return [(server, device)] matching the MAC, else every yunmi.kettle.* device."""
    by_mac = []
    by_model = []
    for server in servers:
        for device in collect_devices(conn, server):
            if mac_digits and normalize_mac(device.get("mac")) == mac_digits:
                by_mac.append((server, device))
            elif str(device.get("model") or "").startswith(MODEL_PREFIX):
                by_model.append((server, device))
    return by_mac or by_model


def fetch_keys(conn, server, device):
    """Return (ble_token, beaconkey). ble_token is the device's `token` field; HA needs it."""
    did = str(device.get("did") or "")
    ble_token = str(device.get("token") or "")
    if not BLE_TOKEN.fullmatch(ble_token):
        raise Fatal(EXIT_NOT_FOUND, f"у устройства {did or '?'} поле token отсутствует или не 24 hex-символа")
    beaconkey = ""
    if "blt" in did:
        response = conn.get_beaconkey(server, did)
        beaconkey = str(((response or {}).get("result") or {}).get("beaconkey") or "")
        if not beaconkey:
            print(f"Предупреждение: облако не вернуло beaconkey для {did}; для HA он не нужен.", file=sys.stderr)
    return ble_token, beaconkey


def describe(server, device):
    return f"{device.get('model', '?')}  MAC {device.get('mac', '?')}  name {device.get('name', '?')!r}  server {server}"


def choose(candidates, ask):
    if len(candidates) == 1:
        return candidates[0]
    print("Найдено несколько подходящих устройств:")
    for index, (server, device) in enumerate(candidates, 1):
        print(f"  {index}) {describe(server, device)}")
    while True:
        answer = ask(f"Номер [1-{len(candidates)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            return candidates[int(answer) - 1]


def prompt_server(servers, default, ask):
    valid = [*servers, ALL_SERVERS]
    while True:
        answer = ask(f"Регион Xiaomi Cloud ({', '.join(valid)}) [{default}]: ").strip().lower() or default
        if answer in valid:
            return answer
        print(f"Неизвестный регион: {answer}")


def obtain_credentials(mod, args, ask, ask_secret):
    """Return (username, password, server, from_disk). Saved credentials skip every prompt."""
    saved = load_credentials()
    valid_servers = (*mod.SERVERS, ALL_SERVERS)
    saved_server = saved.get("XIAOMI_SERVER")
    if saved_server not in valid_servers:
        saved_server = None
    if saved.get("XIAOMI_USERNAME") and saved.get("XIAOMI_PASSWORD"):
        print(f"Использую сохранённые учётные данные из {CONFIG_DIR / CREDENTIALS_NAME} (сбросить: --forget).")
        return (saved["XIAOMI_USERNAME"], saved["XIAOMI_PASSWORD"],
                args.region or saved_server or DEFAULT_SERVER, True)
    default_user = saved.get("XIAOMI_USERNAME", "")
    prompt = f"Логин Xiaomi (email / телефон / ID) [{default_user}]: " if default_user else "Логин Xiaomi (email / телефон / ID): "
    username = ask(prompt).strip() or default_user
    if not username:
        raise Fatal(EXIT_USAGE, "логин не введён")
    server = args.region or prompt_server(mod.SERVERS, saved_server or DEFAULT_SERVER, ask)
    password = ask_secret("Пароль Xiaomi (ввод скрыт): ")
    if not password:
        raise Fatal(EXIT_USAGE, "пароль не введён")
    return username, password, server, False


def login(mod, username, password):
    # The upstream connector reads credentials from its parsed-args namespace.
    mod.args.username = username
    mod.args.password = password
    try:
        connector = mod.PasswordXiaomiCloudConnector()
        apply_default_timeout(connector._session)
        logged_in = connector.login()
    except Exception as exc:  # noqa: BLE001 - exception text may embed the password hash in a URL
        raise Fatal(EXIT_LOGIN, f"ошибка при входе ({type(exc).__name__}); проверьте сеть и повторите") from None
    finally:
        mod.args.password = None
    if not logged_in:
        raise Fatal(EXIT_LOGIN, "вход не удался (неверный логин/пароль, капча или 2FA не пройдены)")
    return connector


def run_cloud_flow(mod, args, ask, ask_secret):
    username, password, server_choice, from_disk = obtain_credentials(mod, args, ask, ask_secret)
    try:
        connector = login(mod, username, password)
    except Fatal as exc:
        if from_disk:
            raise Fatal(exc.code, f"{exc} Сохранённые данные неверны? Сбросить: ./get-kettle-token.sh --forget") from None
        raise
    print("Вход выполнен.")
    if not from_disk:
        save_credentials(username, password, server_choice)
        print(f"ВНИМАНИЕ: логин и пароль сохранены ОТКРЫТЫМ ТЕКСТОМ в {CONFIG_DIR / CREDENTIALS_NAME} (права 600),")
        print("чтобы повторный запуск не спрашивал их. Удалить: ./get-kettle-token.sh --forget")

    mac_digits = normalize_mac(args.mac)
    servers = list(mod.SERVERS) if server_choice == ALL_SERVERS else [server_choice]
    try:
        candidates = find_candidates(connector, servers, mac_digits)
        if not candidates and server_choice != ALL_SERVERS:
            answer = ask(f"В регионе '{server_choice}' чайник не найден. Проверить остальные регионы? [Y/n]: ").strip().lower()
            if answer in ("", "y", "yes", "д", "да"):
                rest = [s for s in mod.SERVERS if s != server_choice]
                candidates = find_candidates(connector, rest, mac_digits)
        if not candidates:
            raise Fatal(EXIT_NOT_FOUND, f"устройство с MAC {format_mac(mac_digits)} или model {MODEL_PREFIX}* не найдено на аккаунте")
        server, device = choose(candidates, ask)
        ble_token, beaconkey = fetch_keys(connector, server, device)
    except Fatal:
        raise
    except Exception as exc:  # noqa: BLE001
        raise Fatal(EXIT_LOGIN, f"ошибка запроса к облаку ({type(exc).__name__}); повторите") from None

    mac = format_mac(normalize_mac(device.get("mac"))) if normalize_mac(device.get("mac")) else args.mac
    model = str(device.get("model") or "")
    path = save_kettle(mac, model, ble_token, beaconkey, str(device.get("did") or ""))
    if server != server_choice and server_choice != ALL_SERVERS:
        save_credentials(username, password, server)

    print()
    print(f"MAC:       {mac}")
    print(f"MODEL:     {model}")
    print(f"BLE TOKEN: {ble_token.lower()}   (его вводить в Home Assistant)")
    if beaconkey:
        print(f"BEACONKEY: {beaconkey}")
    print()
    print(f"Токен чайника сохранён в {path} (права 600).")
    print("Дальше: ./show-ha-setup.sh")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Получить MAC, model и BLE-токен чайника yunmi.kettle.* из Xiaomi Cloud. "
                    "Логин и пароль вводятся интерактивно один раз и сохраняются открытым текстом "
                    f"в {CONFIG_DIR / CREDENTIALS_NAME} (права 600); --forget удаляет их.",
        epilog="Коды выхода: 0 ок, 2 использование/нет терминала, 3 extractor/зависимости, "
               "4 вход или запрос к облаку, 5 чайник не найден, 130 прервано.",
    )
    parser.add_argument("--extractor", type=Path, help="путь к token_extractor.py (передаёт get-kettle-token.sh)")
    parser.add_argument("--mac", help="MAC чайника; без флага ищется по модели yunmi.kettle.*")
    parser.add_argument("--region", help="регион Xiaomi Cloud (cn, de, us, ru, tw, sg, in, i2 или all); без флага берётся сохранённый или спрашивается")
    parser.add_argument("--forget", action="store_true", help="удалить сохранённые логин/пароль и выйти")
    parser.add_argument("--check", action="store_true",
                        help="только проверить, что extractor импортируется; без сети и без ввода логина")
    return parser


def main(argv=None, ask=input, ask_secret=getpass.getpass):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.forget:
            forget_credentials()
            return EXIT_OK
        if args.mac and not normalize_mac(args.mac):
            raise Fatal(EXIT_USAGE, f"некорректный MAC: {args.mac!r}")
        if args.extractor is None:
            raise Fatal(EXIT_USAGE, "нужен --extractor PATH (обычно запускайте через get-kettle-token.sh)")
        mod = load_extractor(args.extractor)
        if args.region and args.region not in (*mod.SERVERS, ALL_SERVERS):
            raise Fatal(EXIT_USAGE, f"неизвестный регион {args.region!r}; допустимо: {', '.join([*mod.SERVERS, ALL_SERVERS])}")
        if args.check:
            print(f"OK: extractor импортируется (python {sys.version.split()[0]}, регионы: {', '.join(mod.SERVERS)})")
            return EXIT_OK
        if ask is input and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise Fatal(EXIT_USAGE, "нужен интерактивный терминал: логин и пароль вводятся вручную")
        run_cloud_flow(mod, args, ask, ask_secret)
        return EXIT_OK
    except Fatal as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return exc.code
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
