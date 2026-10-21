# kettle-xiaomi

Умный чайник Xiaomi (`yunmi.kettle.v14`) в Home Assistant: получение BLE-токена из Xiaomi Cloud, Bluetooth-прокси на ESP32 (Lolin D32), блюпринты автоматизации и рабочая станция ESP32-S3.

```
scripts/     get-kettle-token.sh, get_kettle_token.py, show-ha-setup.sh, flash-lolin.sh, requirements.txt
esphome/     прошивки ESPHome: lolin-d32-btproxy.yaml (чистый BLE-прокси), station*.yaml, d32/*.yaml; secrets.example.yaml
docs/        lolin-d32.md: справочник по плате и распиновке
blueprints/  kettle_auto_off.yaml, kettle_plug_keep_on.yaml и README с подробным описанием
patches/     yunmi_kettle-dynamic-interval.patch: быстрый опрос компонента yunmi_kettle во время нагрева (см. blueprints/README.md)
captures/    parse_btsnoop.py: разбор HCI snoop-лога Android
tests/       офлайн-тесты скриптов
```

Скрипты получают из Xiaomi Cloud **MAC, model и BLE-токен** чайника (24 hex) и показывают, что вводить в Home Assistant. MAC можно указать флагом `--mac`; без него чайник ищется на аккаунте по модели `yunmi.kettle.*`.

## Запуск

```bash
scripts/get-kettle-token.sh      # первый раз спросит логин, регион, пароль Xiaomi; дальше берёт сохранённые
scripts/show-ha-setup.sh         # напечатает, что вводить в HA (без сети)
```

Нужны `bash`, `curl`, `uv` или `python3 >= 3.9` (с модулем `venv`), доступ к GitHub и PyPI при первом запуске.

Что делает `scripts/get-kettle-token.sh`:

1. Создаёт venv в `~/.cache/kettle-xiaomi/venv` (через `uv`, если есть, иначе `python3 -m venv`) и ставит зависимости из `scripts/requirements.txt`. Повторный запуск ничего не скачивает.
2. Скачивает [Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor) **v1.5.1** (коммит `c5cc5a4c1a65…`) и сверяет SHA-256. Это один файл `token_extractor.py`, не pip-пакет; при несовпадении суммы он не запускается.
3. Запускает `get_kettle_token.py`: он импортирует классы extractor и берёт логин/регион/пароль из `~/.config/kettle-xiaomi/credentials` либо (первый раз) спрашивает их (регион по умолчанию `de`; `cn, de, us, ru, tw, sg, in, i2` или `all`; пароль вводится скрыто через `getpass`). Входит в Xiaomi Cloud, ищет на аккаунте устройство с нужным MAC либо `model = yunmi.kettle.*`, по строке прогресса на регион (`de: homes=N, devices=M`).
4. Печатает MAC, model, токен и пишет их в `~/.config/kettle-xiaomi/kettle.env`.

Все сетевые запросы extractor получают таймаут (подключение 10 с, ответ 30 с): при зависании сервера скрипт завершится с ошибкой, а не повиснет.

Опции (`scripts/get-kettle-token.sh --help`): `--mac MAC`, `--region REGION`, `--forget` (удалить сохранённые логин/пароль и выйти; сеть не нужна), `--check` (только проверить, что окружение собрано и extractor импортируется: без сети и без ввода логина).

Капча и 2FA (код из письма) обрабатывает сам extractor интерактивно: капчу покажет по адресу `http://127.0.0.1:31415` (на время ввода порт открыт на всех интерфейсах), код 2FA попросит в терминале. Если в выбранном регионе чайника нет, скрипт предложит проверить остальные.

## Что сохраняется и где

Каталог `~/.config/kettle-xiaomi/` (права `700`), файлы `600`, запись атомарная:

| Файл | Содержимое |
| --- | --- |
| `kettle.env` | `KETTLE_MAC`, `KETTLE_MODEL`, `KETTLE_DID`, `KETTLE_BLE_TOKEN` (24 hex, его вводят в HA), `KETTLE_BEACONKEY` (32 hex, если облако его вернуло) |
| `credentials` | `XIAOMI_USERNAME`, `XIAOMI_PASSWORD`, `XIAOMI_SERVER` |

**Логин и пароль Xiaomi лежат в `credentials` открытым текстом** (защита только правами `600`), чтобы повторный запуск не спрашивал их. Файл создаётся после первого успешного входа; скрипт один раз печатает об этом предупреждение. Пароль никогда не передаётся через аргументы командной строки или переменные окружения и не печатается. Удалить: `scripts/get-kettle-token.sh --forget`.

Файлы старых версий (`account.env`, `kettle.env` с ключами `KETTLE_TOKEN` / `KETTLE_TOKEN_KIND`) скриптами больше не читаются: `account.env` можно удалить, `kettle.env` перезапишется при следующем запуске.

## Безопасность

- Токен — секрет: с ним можно управлять чайником по BLE. Не публикуйте и не коммитьте; `.gitignore` исключает `*.env`, `credentials` и `secrets.yaml`.
- Пароль в `credentials` не зашифрован. Если это неприемлемо, выполните `scripts/get-kettle-token.sh --forget` после получения токена: он остаётся в `kettle.env`.
- Логин идёт напрямую на серверы Xiaomi (`account.xiaomi.com`, `*.api.io.mi.com`) из кода extractor; сторонних серверов нет.
- Тексты исключений при входе скрыты: в них могут оказаться URL с хэшем пароля.
- Реальный вход проверяйте сами: автоматические тесты работают только на подменённом коннекторе.

## Откуда токен

Из Xiaomi Cloud: extractor входит под вашим аккаунтом и получает список устройств. Для Home Assistant нужно поле **`token`** устройства из этого списка (12 байт = 24 hex): оно сохраняется как `KETTLE_BLE_TOKEN`. Для BLE-устройства (`did` вида `blt.*`) дополнительно запрашивается `beaconkey` (32 hex, `KETTLE_BEACONKEY`); интеграция HA его **не принимает** (ответ `invalid_token`). Если поле `token` не 24 hex, скрипт ничего не сохраняет и сообщает об этом.

## Home Assistant

`scripts/show-ha-setup.sh` напечатает MAC и `KETTLE_BLE_TOKEN` для: Settings → Devices & Services → Add Integration → Yunmi Kettle. Если в `kettle.env` нет `KETTLE_BLE_TOKEN` или он не 24 hex, скрипт завершится с понятной ошибкой (код 3).

**Mi Home нужно закрыть на время работы HA**: приложение может занять BLE-подключение к чайнику, и HA не подключится.

## Удалить сохранённое

```bash
scripts/get-kettle-token.sh --forget    # только логин/пароль (credentials)
rm -rf ~/.config/kettle-xiaomi    # всё: логин/пароль, MAC/model/токены
rm -rf ~/.cache/kettle-xiaomi     # venv и скачанный extractor
```

Если чайник удалили из Mi Home или сбросили и привязали заново, токен нужно получить заново (запустите `scripts/get-kettle-token.sh` ещё раз).

## Коды выхода

`0` ок · `2` неверные аргументы / нет интерактивного терминала · `3` проблема с окружением или extractor · `4` вход или запрос к облаку не удался · `5` чайник не найден · `130` прервано.

## Тесты

```bash
~/.cache/kettle-xiaomi/venv/bin/python -m unittest discover -s tests
```

Нужен Python с `requests` (venv создаётся после первого `scripts/get-kettle-token.sh`). Тесты офлайн, на подменённом коннекторе; сеть и реальные учётные данные не нужны, настоящий `~/.config` не затрагивается.

## Bluetooth-прокси на ESP32 (Lolin D32)

Чайник общается по BLE; ESPHome Bluetooth proxy на ESP32 приближает радио к нему. Подробности по плате: `docs/lolin-d32.md`.

```bash
cp esphome/secrets.example.yaml esphome/secrets.yaml   # впишите Wi-Fi, ключ API, пароль OTA (файл в .gitignore)
esphome compile esphome/lolin-d32-btproxy.yaml
scripts/flash-lolin.sh [--erase] [--port /dev/ttyUSB0] [--bin path]  # по умолчанию берёт свежую сборку из esphome/.esphome/build/
```

`esphome/station.yaml` и `esphome/station-d32.yaml` добавляют к прокси станцию учёта катушек (PN532, OLED, энкодер, HX711, датчики воздуха) и ссылаются на сервис `3d-api`: в `api_base` подставьте адрес своего хоста. Каталоги `esphome/.esphome/` и `esphome/build/` (кэш и сборки) в репозиторий не попадают: кэш хранит расшифрованные значения `!secret`.

## Блюпринты

`blueprints/kettle_auto_off.yaml`: упрощённая логика «нагрев → предсказание момента достижения цели → короткое выключение розетки (импульс)»; удержание температуры не используется (нижнюю кнопку чайника нажимают вручную, блюпринт реагирует только на статус `heating`). `blueprints/kettle_plug_keep_on.yaml` возвращает питание розетки после обрыва. Скопируйте их в `<config>/blueprints/automation/kettle/`; сущности по умолчанию и настройка описаны в `blueprints/README.md`.

Для предсказания нужен частый опрос чайника: `patches/yunmi_kettle-dynamic-interval.patch` опрашивает его каждые 5 с во время нагрева (только статус и температура) и возвращает обычный интервал в остальное время. Патч содержит только diff (не копию кода) и применяется к вашей копии [opravdin/hass-yunmi-kettle-ble](https://github.com/opravdin/hass-yunmi-kettle-ble) (MIT, автор opravdin), которая в этот репозиторий не входит. Там же: `switch.kettle_keep_warm_after_boil` в этой интеграции назван неточно, это память удержания после подъёма (`siid 3 / piid 2`), а не режим кипячения. Живой нагрев с патчем не проверялся.

## Благодарности

- [opravdin/hass-yunmi-kettle-ble](https://github.com/opravdin/hass-yunmi-kettle-ble): интеграция HA для чайника (в репозиторий не скопирована).
- [asruda/mi_kettle_pro](https://github.com/asruda/mi_kettle_pro): разбор протокола чайника.
- [PiotrMachowski/Xiaomi-cloud-tokens-extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor): получение токенов; скачивается при запуске по зафиксированному коммиту с проверкой SHA-256.
- [eigger/hass-catprinter](https://github.com/eigger/hass-catprinter): протокол мини-принтера, использованный в смежной работе (`ha-tinyprint`).

## Лицензия

MIT, см. `LICENSE`.
