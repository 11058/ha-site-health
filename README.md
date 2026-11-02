# Site Health — здоровье сети MikroTik в Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![Validate](https://github.com/11058/ha-site-health/actions/workflows/validate.yml/badge.svg)](https://github.com/11058/ha-site-health/actions/workflows/validate.yml)

Одна страница в сайдбаре HA — «Здоровье сети» — для объекта, где вся сеть
построена на MikroTik (RouterOS 7):

- **Каналы связи** — WAN-порты (DHCP-клиенты), скорость приёма/передачи,
  проверки netwatch (пинг, потери), туннели WireGuard (давность handshake).
- **Устройства** — главный роутер и остальные MikroTik (радиобаза, мосты, точки):
  на связи, CPU, температура, аптайм, версия.
- **Радиомосты** — уровень сигнала и скорости по каждому линку.
- **Клиенты по сегментам** — Семья / Гости / Умный дом / Видео / Админ / …
  (по VLAN), кто в сети, через какой Wi-Fi, с каким сигналом.
- **Список домашних** — кнопки «В домашние» / «В гости»: MAC добавляется в
  `/interface wifi access-list` главного роутера с `vlan-id` домашнего VLAN,
  клиент переподключается. Новые устройства по умолчанию остаются в гостевой сети.

Плюс обычные сущности HA (сенсоры, binary_sensor) для истории и автоматизаций,
и сервисы `site_health.add_family_device` / `remove_family_device` / `refresh`.

> В планах: 4G-роутер Тандем-4GT-OEM (две SIM: активная, состояние, сигнал).

## Как это работает

Интеграция опрашивает RouterOS по **REST API** (`www-ssl`). Скриптов на
роутерах не нужно: только служба `www-ssl`, отдельный пользователь и, для
проверки каналов, записи `netwatch`. Логин и пароль вводятся **отдельно для
каждого устройства** и хранятся в HA.

Устройства находит сам главный роутер (`/ip neighbor`): при добавлении нового
устройства выбираете его из списка найденных MikroTik.

## Подготовка MikroTik

1. Главный роутер: `mikrotik/setup-core.rsc` — включает `www-ssl` только для
   адреса HA, создаёт группу `ha-infra` (чтение + запись, без ssh/winbox/policy/
   sensitive), правила firewall для HA и netwatch-пробы интернета.
2. Остальные MikroTik: `mikrotik/setup-device.rsc` — то же, но группа только на чтение.
3. На каждом устройстве создайте пользователя:
   `/user add name=ha-infra group=ha-infra address=<IP HA>/32 password="..."`

Просмотрите скрипты перед вставкой: адреса и имена интерфейсов в них для
объекта «Береговая 3».

## Установка

1. HACS → Integrations → ⋮ → Custom repositories → `https://github.com/11058/ha-site-health`, тип *Integration*.
2. Установить **Site Health**, перезапустить HA.
3. Настройки → Устройства и службы → Добавить → *Site Health*: название объекта,
   адрес главного роутера, логин и пароль.
4. В карточке интеграции — **Добавить устройство** для каждой радиобазы/моста.
5. В сайдбаре появится **Здоровье сети**.

## Разработка

```bash
uv venv -p 3.13 && uv pip install homeassistant pytest ruff
pytest tests -q && ruff check . && ruff format --check .
```

Тесты не требуют HA и роутера: парсеры проверяются на фикстурах в формате REST
RouterOS 7 (`tests/fixtures/`).
