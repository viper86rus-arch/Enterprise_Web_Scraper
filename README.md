# 🕷️ Enterprise Async Web Scraper & Catalog Platform

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-45ba4b.svg?logo=playwright&logoColor=white)](https://playwright.dev/python/)
[![WebSockets](https://img.shields.io/badge/WebSockets-Live_Logs-orange.svg)](https://websockets.readthedocs.io/)
[![Pydantic v2](https://img.shields.io/badge/Pydantic-v2.0%2B-e92063.svg)](https://docs.pydantic.dev/)

> **Промышленная платформа для сбора, валидации и мониторинга данных с динамических веб-каталогов.**
> Поддерживает два независимых режима работы: **CLI-ядро** (`main.py`) для автоматизации и скриптов и **Web UI Dashboard** (`app.py`) с интерактивным интерфейсом и реал-тайм стримингом логов через WebSocket.

---

## 📑 Содержание

- [Два режима работы — быстрый старт](#-два-режима-работы--быстрый-старт)
- [Установка и подготовка](#-установка-и-подготовка)
- [Режим 1: Web UI Dashboard](#1-web-ui-dashboard-apppy)
- [Режим 2: CLI-ядро](#2-cli-ядро-mainpy)
- [Архитектура Web Shell (`app.py`)](#-архитектура-web-shell-apppy)
- [Ключевые особенности CLI-ядра](#-ключевые-особенности-cli-ядра)
- [Структура проекта](#-структура-проекта)
- [Зависимости](#-зависимости)
- [Формат файла прокси](#-формат-файла-прокси)
- [Разработка кастомных адаптеров](#-разработка-кастомных-адаптеров)
- [Техническая спецификация](#-техническая-спецификация)

---

## ⚡ Два режима работы — быстрый старт

### Режим 1 — Web UI Dashboard (рекомендуется для операторов)
```bash
python app.py
# Откройте в браузере: http://localhost:8000
```

### Режим 2 — CLI-ядро (для автоматизации, cron, CI/CD)
```bash
python main.py --pages 5 --concurrency 3
```

---

## 🚀 Установка и подготовка

### Требования
- Python **3.10+**
- ~300 МБ свободного места для бинарника Chromium

### 1. Создать и активировать виртуальное окружение
```bash
python -m venv .venv

# Windows (PowerShell):
.venv\Scripts\Activate.ps1

# Linux / macOS:
source .venv/bin/activate
```

### 2. Установить зависимости
```bash
pip install -r requirements.txt
```

### 3. Скачать браузер Playwright
```bash
playwright install chromium
```
> На Linux-серверах без GUI при необходимости: `playwright install-deps chromium`

---

## 1. Web UI Dashboard (`app.py`)

Запуск сервера:

```bash
python app.py
```

Откройте в браузере: **[http://localhost:8000](http://localhost:8000)**

### Что доступно в интерфейсе
- **Настройка параметров:** количество страниц, уровень параллелизма, имя Excel-файла и путь к SQLite
- **Запуск одной кнопкой:** неблокирующий фоновый старт — UI реагирует мгновенно
- **Live-терминал логов:** каждое действие ядра (открытие URL, сбор карточек, сохранение в БД) отображается в реальном времени через WebSocket
- **Индикатор статуса:** анимированный бейдж меняется по состоянию (`Готов` → `Выполняется` → `Завершено`)
- **Скачивание отчёта:** кнопка «Скачать Excel» активируется автоматически по завершении задачи

---

## 2. CLI-ядро (`main.py`)

Запуск из терминала:

```bash
python main.py [АРГУМЕНТЫ]
```

### Аргументы командной строки

| Аргумент | Тип | По умолчанию | Описание |
|---|:---:|:---:|---|
| `--pages` | `int` | `3` | Количество страниц каталога для обхода |
| `--concurrency` | `int` | `3` | Максимальное число параллельных вкладок браузера |
| `--output` | `str` | `demo_catalog.xlsx` | Имя или путь к выходному Excel-файлу |
| `--db` | `str` | `scraped_data.db` | Путь к SQLite-базе (передайте `""` для отключения) |
| `--proxies` | `str` | `None` | Путь к текстовому файлу со списком прокси |
| `--headful` | flag | `False` | Видимое окно браузера (режим отладки) |

### Примеры запуска

```bash
# Быстрый тест — 3 страницы, 3 потока
python main.py --pages 3 --concurrency 3

# Высокопроизводительный сбор — 20 страниц, 6 потоков
python main.py --pages 20 --concurrency 6 --output reports/catalog_full.xlsx

# Боевой запуск с пулом прокси
python main.py --pages 50 --concurrency 8 --proxies proxies.txt --db production.db

# Отладка в видимом браузере
python main.py --pages 2 --concurrency 1 --headful

# Только Excel, без сохранения в БД
python main.py --pages 10 --db "" --output fast_export.xlsx
```

---

## 🌐 Архитектура Web Shell (`app.py`)

Файл `app.py` реализует тонкую и эффективную веб-надстройку над ядром `main.py` на базе FastAPI.

### REST и WebSocket эндпоинты

| Метод | Путь | Назначение |
|---|---|---|
| `GET` | `/` | Отдача HTML-интерфейса (читается через `Path.read_text()` → `HTMLResponse`) |
| `POST` | `/api/run` | Неблокирующий старт скрапера через `asyncio.create_task` |
| `GET` | `/api/status` | Проверка состояния `TaskState` и наличия готового файла |
| `GET` | `/api/download` | Скачивание сгенерированного `.xlsx` через `FileResponse` |
| `WS` | `/ws/logs` | Стриминг строк лога в реальном времени |

### Неблокирующий запуск (`POST /api/run`)

При получении запроса:
1. Проверяется флаг `state.running` — если задача уже выполняется, возвращается ответ `"already_running"` без дублирования процесса.
2. Очередь `state.log_queue` очищается от данных предыдущего прогона.
3. Запускается `asyncio.create_task(_run_scrape_task(params))` — скрапинг работает в фоне, а эндпоинт немедленно возвращает `{"status": "started"}`.

### Перехват логов (`WebSocketLogger` + `asyncio.Queue`)

Чтобы вывод из `ScrapingEngine` попадал в браузер, реализован перехват на уровне stdout движка:

```python
class WebSocketLogger(io.TextIOBase):
    """Перенаправляет print/stdout в asyncio-очередь для трансляции в WS."""
    def __init__(self, queue: asyncio.Queue): ...
    def write(self, text: str) -> int:
        clean = _ANSI_ESCAPE.sub("", text)          # убираем ANSI escape-коды
        clean = re.sub(r"\[/?[^\]]*\]", "", clean)  # убираем Rich-теги [cyan], [bold], ...
        if clean.strip():
            loop.call_soon_threadsafe(self._queue.put_nowait, clean)
```

На время сессии `_main_module.console` заменяется на `Console(file=WebSocketLogger(queue))`:
```python
ws_console = _make_ws_console(queue)
_main_module.console = ws_console  # перехват Rich-вывода ядра
# ... скрапинг ...
# finally: гарантированное восстановление
_main_module.console = Console()
```

### WebSocket стриминг (`WS /ws/logs`)

```
Клиент ←─── {"type": "log",  "text": "Сбор страницы 3/5..."}  ─── сервер
Клиент ←─── {"type": "ping"}                                  ─── (каждую секунду при простое)
Клиент ←─── {"type": "log",  "text": "__DONE__"}              ─── (маркер завершения)
```

- Хэндлер извлекает строки из `state.log_queue` с таймаутом 1 с (`asyncio.wait_for(..., timeout=1.0)`).
- При таймауте отправляется keep-alive пинг `{"type": "ping"}`, удерживающий соединение живым через прокси и файрволы.
- По завершении задачи в очередь кладётся маркер `__DONE__`, сигнализирующий UI об активации кнопки скачивания.

---

## ⚡ Ключевые особенности CLI-ядра

### 1. Асинхронный пул и контроль нагрузки
- Все страницы обрабатываются конкурентно через `asyncio.gather`.
- Жёсткий лимит параллелизма: `asyncio.Semaphore(concurrency)` — защита от исчерпания RAM и Rate-Limit на стороне сервера.

### 2. Строгая валидация данных (`Pydantic v2`)
Каждая карточка проходит через модель `ProductItem`:
- **`parse_price`** — вырезает символы валют, нормализует разделитель `,` → `.`, приводит к `float`.
- **`clean_title`** — схлопывает множественные пробелы/переносы, отсекает пустые строки.
- **`validate_url`** — проверяет наличие схемы `http://` / `https://`.

Невалидные карточки безопасно логируются и пропускаются без прерывания всего процесса.

### 3. Stealth-режим (`Playwright`)
- Патчинг `navigator.webdriver` и `window.chrome` через `add_init_script`.
- Реалистичный User-Agent, Viewport, Locale, Timezone.
- **Human Scroll:** `mouse.wheel` с рандомизированными шагами (350–650 px) и паузами (0.3–0.6 с) для Lazy-Load.
- Случайные задержки 0.8–2.0 с между страницами.

### 4. Блокировка медиа-трафика (`Route Interception`)
Блокируются `image`, `media`, `font`, `stylesheet`:
- **Ускорение в 3–5 раз** за счёт мгновенной загрузки DOM без графики.
- **Экономия трафика** прокси (не расходуется на изображения).

### 5. SQLite без блокировок (`asyncio.to_thread`)
- Все дисковые операции вынесены в `asyncio.to_thread`, event loop не блокируется.
- Дедупликация: `INSERT OR IGNORE` по первичному ключу `sku`.

### 6. Ротация прокси (`ProxyManager`)
- Циклическая ротация через `itertools.cycle`.
- Поддержка HTTP/SOCKS5 с авторизацией, включая `@` в теле пароля.

### 7. Excel-отчёты (`OpenPyXL`)
- Корпоративная шапка (`#1F497D`), белый жирный шрифт, сетка ячеек.
- Финансовый формат: `#,##0.00`.
- Автоподбор ширины столбцов [12, 50] символов.

---

## 📁 Структура проекта

```text
Parser_test2/
├── app.py                 # FastAPI-сервер: REST API + WebSocket + отдача UI
├── main.py                # CLI-ядро: ScrapingEngine, адаптеры, Pydantic, CLI
├── templates/
│   └── index.html         # Веб-интерфейс (тёмная тема, Live-логи, управление)
├── requirements.txt       # Зависимости проекта
├── demo_catalog.xlsx      # Эталонный Excel-отчёт (100 записей, создан main.py)
├── scraped_data.db        # SQLite-база (создаётся автоматически)
├── .gitignore
└── README.md
```

---

## 📦 Зависимости

| Пакет | Версия | Назначение |
|---|---|---|
| `fastapi` | ≥ 0.111.0 | Асинхронный веб-фреймворк: REST API и WebSocket-эндпоинты |
| `uvicorn[standard]` | ≥ 0.29.0 | ASGI-сервер с поддержкой UVLoop и протокола WebSocket |
| `websockets` | ≥ 12.0 | Библиотека WebSocket-протокола (используется uvicorn) |
| `playwright` | ≥ 1.40.0 | Headless-автоматизация Chromium, stealth, перехват трафика |
| `pydantic` | ≥ 2.0.0 | Строгая типизация и валидация данных (`ProductItem`, `ScrapeRequest`) |
| `openpyxl` | ≥ 3.1.0 | Генерация стилизованных Excel-отчётов |
| `rich` | ≥ 13.0.0 | Форматированный терминальный вывод (перехватывается в веб-режиме) |

> **Стандартная библиотека** (`asyncio`, `sqlite3`, `argparse`, `re`, `pathlib`, `io`) — без установки.

---

## 🔧 Формат файла прокси

Один адрес на строку; строки с `#` — комментарии:

```text
# HTTP без авторизации
http://185.199.229.156:7492

# SOCKS5 с авторизацией (поддерживается @ в пароле)
socks5://user:P@ss!word@194.67.210.12:1080

# HTTP с авторизацией
http://admin:secret@10.0.0.5:3128
```

---

## 🔒 Разработка кастомных адаптеров

> Публичный репозиторий использует демо-адаптер `DemoBooksAdapter` ([books.toscrape.com](https://books.toscrape.com)).

Для подключения реальной площадки нужно унаследовать `BaseCatalogAdapter` и реализовать 3 метода:

```python
from main import BaseCatalogAdapter, ProductItem, ScrapingEngine
from playwright.async_api import Page
from typing import List

class MyShopAdapter(BaseCatalogAdapter):

    @property
    def target_name(self) -> str:
        return "MyShop_Production"

    async def build_catalog_url(self, page_number: int) -> str:
        return f"https://myshop.ru/catalog?page={page_number}"

    async def extract_items(self, page: Page) -> List[ProductItem]:
        items = []
        for card in await page.query_selector_all(".product-card"):
            try:
                items.append(ProductItem(
                    sku=await card.get_attribute("data-sku"),
                    title=await (await card.query_selector(".title")).inner_text(),
                    price=await (await card.query_selector(".price")).inner_text(),
                    url=await (await card.query_selector("a")).get_attribute("href"),
                ))
            except Exception:
                continue
        return items

# Запуск через движок:
engine = ScrapingEngine(adapter=MyShopAdapter(), max_concurrency=5)
await engine.run(total_pages=10)
```

### Типовые коммерческие сценарии

| Сценарий | Реализация |
|---|---|
| B2B-каталоги (1C, SAP) | Парсинг табличного HTML, сопоставление артикулов |
| Маркетплейсы под Cloudflare / DataDome | Управление fingerprint-профилем |
| Авторизация и сессии | Пробрасывание cookies / JWT до начала сбора |
| Перехват внутренних JSON API | `page.on("response", ...)` — без CSS-селекторов |
| Интеграция с внешними системами | PostgreSQL, ClickHouse, S3, 1C через API |

---

## 📋 Техническая спецификация

| Параметр | Значение |
|---|---|
| **Язык** | Python 3.10+ |
| **Веб-сервер** | FastAPI + Uvicorn (ASGI) |
| **Коммуникация браузер → сервер** | WebSocket (полнодуплексный, с keep-alive пингами) |
| **Фронтенд** | HTML5 / CSS3 / Vanilla JS (тёмная тема, без фреймворков) |
| **Движок браузера** | Playwright (Chromium) |
| **Параллелизм** | `asyncio` + `asyncio.Semaphore` |
| **Anti-Detect / Stealth** | Патчинг `navigator.webdriver`, подмена UA/Viewport, Human Scroll |
| **Фильтрация трафика** | Route Interception: `image`, `media`, `font`, `stylesheet` |
| **Схема данных** | Pydantic v2 (`field_validator`) |
| **Хранилище** | SQLite3 (`asyncio.to_thread`, `INSERT OR IGNORE`) |
| **Формат выгрузки** | Excel `.xlsx` (OpenPyXL, стилизованные таблицы) |
| **Прокси** | HTTP / SOCKS5 с авторизацией, `itertools.cycle` |

---

*Документация соответствует версиям `main.py` и `app.py`, представленным в репозитории.*