#!/usr/bin/env bash
# Print what to enter in Home Assistant for the Yunmi Kettle integration.
# Reads ~/.config/kettle-xiaomi/kettle.env only; makes no network requests.
set -euo pipefail

ENV_FILE="${HOME}/.config/kettle-xiaomi/kettle.env"

case "${1:-}" in
  -h | --help)
    cat <<EOF
Использование: ./show-ha-setup.sh

Читает ${ENV_FILE} (создаётся ./get-kettle-token.sh)
и печатает MAC и токен для Home Assistant. Без сети.
Коды выхода: 0 ок, 3 нет/битый файл с данными, 2 неверные аргументы.
EOF
    exit 0
    ;;
  "") ;;
  *)
    echo "Неизвестный аргумент: $1 (см. --help)" >&2
    exit 2
    ;;
esac

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Ошибка: нет ${ENV_FILE}. Сначала запустите ./get-kettle-token.sh" >&2
  exit 3
fi

# Strict KEY=VALUE parser: the file is never sourced/executed.
KETTLE_MAC="" KETTLE_MODEL="" KETTLE_BLE_TOKEN=""
while IFS='=' read -r key value || [[ -n "${key}" ]]; do
  case "${key}" in
    KETTLE_MAC) KETTLE_MAC="${value}" ;;
    KETTLE_MODEL) KETTLE_MODEL="${value}" ;;
    KETTLE_BLE_TOKEN) KETTLE_BLE_TOKEN="${value}" ;;
  esac
done <"${ENV_FILE}"

if [[ -z "${KETTLE_MAC}" || -z "${KETTLE_MODEL}" || -z "${KETTLE_BLE_TOKEN}" ]]; then
  echo "Ошибка: в ${ENV_FILE} не хватает KETTLE_MAC / KETTLE_MODEL / KETTLE_BLE_TOKEN (файл старого формата?). Запустите ./get-kettle-token.sh заново" >&2
  exit 3
fi

# Home Assistant's Yunmi Kettle integration accepts only the 12-byte device token (24 hex), not the 32-hex beaconkey.
if [[ ! "${KETTLE_BLE_TOKEN}" =~ ^[0-9a-fA-F]{24}$ ]]; then
  echo "Ошибка: KETTLE_BLE_TOKEN в ${ENV_FILE} должен быть 24 hex-символа (сейчас ${#KETTLE_BLE_TOKEN} симв.). Запустите ./get-kettle-token.sh заново" >&2
  exit 3
fi

cat <<EOF
Чайник: ${KETTLE_MODEL}

В Home Assistant:
  Settings → Devices & Services → Add Integration → Yunmi Kettle

  MAC:   ${KETTLE_MAC}
  Токен: ${KETTLE_BLE_TOKEN}

Перед этим закройте приложение Mi Home (на телефоне и везде, где оно открыто):
иначе оно может занять BLE-подключение к чайнику, и HA не подключится.
Токен секретный — не публикуйте его и не коммитьте.
EOF
