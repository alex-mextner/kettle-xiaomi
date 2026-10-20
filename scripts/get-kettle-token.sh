#!/usr/bin/env bash
# Prepare a private venv + pinned Xiaomi-cloud-tokens-extractor, then run get_kettle_token.py.
# One command: ./get-kettle-token.sh   (asks for Xiaomi login/password on first run; saved afterwards)
#
# Options are forwarded to get_kettle_token.py (see --help). The extractor is not a pip
# package, so a single file is downloaded from a pinned commit and checked against SHA-256.
set -euo pipefail

EXTRACTOR_REPO="PiotrMachowski/Xiaomi-cloud-tokens-extractor"
EXTRACTOR_REF="c5cc5a4c1a6587a9a3ba1c8ce8b8c95f43118b29" # release v1.5.1
EXTRACTOR_SHA256="e4eb8220df0f68c945c21cde5dbc5a0a276e7a035857b568d9423788ef192a30"
MIN_PYTHON="3.9"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="${HOME}/.cache/kettle-xiaomi"
VENV_DIR="${CACHE_DIR}/venv"
EXTRACTOR_FILE="${CACHE_DIR}/token_extractor-${EXTRACTOR_REF:0:12}.py"
REQUIREMENTS="${SCRIPT_DIR}/requirements.txt"
STAMP="${VENV_DIR}/.kettle-installed"

die() {
  echo "Ошибка: $*" >&2
  exit 3
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | cut -d' ' -f1
  else
    die "нужен sha256sum или shasum"
  fi
}

# Help and --forget must work without any setup or network.
for arg in "$@"; do
  case "${arg}" in
    -h | --help)
      command -v python3 >/dev/null 2>&1 || die "нужен python3 >= ${MIN_PYTHON}"
      echo "Использование: ./get-kettle-token.sh [опции]"
      echo "Создаёт venv в ${CACHE_DIR}, ставит extractor ${EXTRACTOR_REPO}@${EXTRACTOR_REF:0:12}, спрашивает логин/пароль Xiaomi."
      echo
      exec python3 "${SCRIPT_DIR}/get_kettle_token.py" --help
      ;;
    --forget)
      command -v python3 >/dev/null 2>&1 || die "нужен python3 >= ${MIN_PYTHON}"
      exec python3 "${SCRIPT_DIR}/get_kettle_token.py" --forget
      ;;
  esac
done

make_venv() {
  rm -rf "${VENV_DIR}"
  mkdir -p "${CACHE_DIR}"
  if command -v uv >/dev/null 2>&1; then
    uv venv --quiet --python ">=${MIN_PYTHON}" "${VENV_DIR}"
  else
    command -v python3 >/dev/null 2>&1 || die "нужен python3 >= ${MIN_PYTHON} (или uv)"
    python3 -c "import sys; sys.exit(sys.version_info < tuple(map(int, '${MIN_PYTHON}'.split('.'))))" ||
      die "python3 слишком старый, нужен >= ${MIN_PYTHON}"
    python3 -m venv "${VENV_DIR}" || die "не удалось создать venv (на Debian/Ubuntu: apt install python3-venv)"
  fi
}

install_requirements() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --quiet --python "${VENV_DIR}/bin/python" -r "${REQUIREMENTS}"
  else
    "${VENV_DIR}/bin/python" -m pip install --quiet --disable-pip-version-check -r "${REQUIREMENTS}"
  fi
}

fetch_extractor() {
  command -v curl >/dev/null 2>&1 || die "нужен curl"
  local tmp url
  url="https://raw.githubusercontent.com/${EXTRACTOR_REPO}/${EXTRACTOR_REF}/token_extractor.py"
  tmp="$(mktemp "${CACHE_DIR}/token_extractor.XXXXXX")"
  if ! curl --fail --silent --show-error --location --output "${tmp}" "${url}"; then
    rm -f "${tmp}"
    die "не удалось скачать ${url}"
  fi
  if [[ "$(sha256_of "${tmp}")" != "${EXTRACTOR_SHA256}" ]]; then
    rm -f "${tmp}"
    die "контрольная сумма extractor не совпала (ожидалась ${EXTRACTOR_SHA256}); не запускаю"
  fi
  mv "${tmp}" "${EXTRACTOR_FILE}"
}

ensure_environment() {
  local want_stamp
  want_stamp="${EXTRACTOR_REF} $(sha256_of "${REQUIREMENTS}")"
  if [[ -x "${VENV_DIR}/bin/python" && -f "${STAMP}" && "$(cat "${STAMP}")" == "${want_stamp}" &&
    -f "${EXTRACTOR_FILE}" && "$(sha256_of "${EXTRACTOR_FILE}")" == "${EXTRACTOR_SHA256}" ]]; then
    return
  fi
  echo "Готовлю окружение в ${CACHE_DIR} (один раз)..." >&2
  make_venv
  install_requirements || die "не удалось установить зависимости из ${REQUIREMENTS}"
  fetch_extractor
  printf '%s\n' "${want_stamp}" >"${STAMP}"
}

[[ -f "${REQUIREMENTS}" ]] || die "нет ${REQUIREMENTS}"
mkdir -p "${CACHE_DIR}"
ensure_environment

exec "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/get_kettle_token.py" --extractor "${EXTRACTOR_FILE}" "$@"
