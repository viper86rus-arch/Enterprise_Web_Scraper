#!/usr/bin/env python3
"""
🕷️ Enterprise Async Web Scraper & Catalog Extractor (main.py)
=============================================================
Промышленная асинхронная платформа для сбора и мониторинга данных:
- CLI управление флагами запуска (argparse)
- Неблокирующее сохранение в SQLite (sqlite3 + asyncio.to_thread) с защитой от дублей
- Пул циклической ротации прокси (HTTP / SOCKS5)
- Stealth-эмуляция человека и фильтрация тяжелого медиа-трафика (Playwright)
- Строгая валидация и очистка полей по схеме Pydantic v2
- Профессиональная генерация отчетов Excel с авто-стилями и шапками (OpenPyXL)

Автор: V1PER (viper86rus-arch)
Лицензия: MIT
"""


from __future__ import annotations

import argparse
import asyncio
import csv
import ipaddress
import itertools
import random
import re
import socket
import sqlite3
import sys
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.worksheet import Worksheet
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False
    Workbook = None

try:
    from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    Browser = BrowserContext = Page = Route = async_playwright = None

try:
    from pydantic import BaseModel, Field, field_validator
    HAS_PYDANTIC = True
except ImportError:
    HAS_PYDANTIC = False

    def field_validator(field_name, *args, **kwargs):
        def decorator(fn):
            target = getattr(fn, "__func__", fn)
            try:
                target._target_field = field_name
            except Exception:
                pass
            return fn
        return decorator

    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                for attr_name in dir(self.__class__):
                    bound_attr = getattr(self.__class__, attr_name, None)
                    underlying = getattr(bound_attr, "__func__", bound_attr)
                    if callable(bound_attr) and getattr(underlying, "_target_field", None) == k:
                        v = bound_attr(v)
                setattr(self, k, v)

        def model_dump(self):
            return self.__dict__

    def Field(*args, **kwargs):
        default = kwargs.get("default", None)
        default_factory = kwargs.get("default_factory", None)
        return default_factory() if default_factory else default

try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
except ImportError:
    class Console:
        def print(self, *args, **kwargs):
            clean_args = [re.sub(r"\[.*?\]", "", str(a)) for a in args]
            print(*clean_args)
    console = Console()
    Table = None

# Типы медиа-ресурсов, блокируемых для экономии трафика и ускорения сбора
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})

# Черный список хостов и адресов для защиты от SSRF (CWE-918)
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
    "0.0.0.0",
    "127.0.0.1",
    "[::1]",
    "::1",
})


def is_safe_public_url(url: str, allow_private: bool = False) -> bool:
    """
    Строгая валидация URL против SSRF (Server-Side Request Forgery, CWE-918).
    Блокирует file://, gopher://, loopback, link-local, private RFC 1918 и Cloud Metadata IP.
    """
    if not isinstance(url, str):
        return False
    cleaned = url.strip()
    try:
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        host_lower = hostname.lower()
        if host_lower in _BLOCKED_HOSTNAMES:
            return False

        # Защита от loopback префиксов и приватных подсетей RFC 1918 по строке
        if (
            host_lower.startswith((
                "127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
                "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                "172.29.", "172.30.", "172.31.", "169.254.", "0."
            ))
            or host_lower in ("localhost", "0.0.0.0", "::1", "[::1]")
        ):
            return False

        # Проверка альтернативных представлений IP (octal/dword)
        parts = host_lower.split(".")
        if all(p.isdigit() for p in parts) and len(parts) in (1, 4):
            try:
                if len(parts) == 1:
                    ip_val = int(parts[0])
                    ip_obj = ipaddress.IPv4Address(ip_val)
                    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                        return False
                elif len(parts) == 4 and any(p.startswith("0") and len(p) > 1 for p in parts):
                    octal_val = tuple(int(p, 8) if p.startswith("0") else int(p) for p in parts)
                    ip_obj = ipaddress.IPv4Address(f"{octal_val[0]}.{octal_val[1]}.{octal_val[2]}.{octal_val[3]}")
                    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                        return False
            except Exception:
                return False

        # Проверка DNS резолвинга (если сеть доступна)
        if not allow_private:
            try:
                addr_infos = socket.getaddrinfo(hostname, None)
                for _, _, _, _, sockaddr in addr_infos:
                    ip = ipaddress.ip_address(sockaddr[0])
                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_multicast
                        or ip.is_reserved
                        or ip.is_unspecified
                        or str(ip) == "169.254.169.254"
                    ):
                        return False
            except (socket.gaierror, ValueError, OSError):
                pass
        return True
    except Exception:
        return False


# ==============================================================================
# 1. МОДЕЛИ ДАННЫХ (Pydantic v2)
# ==============================================================================

class ProductItem(BaseModel):
    """Строгая схема единицы товара с автоматической очисткой полей."""

    sku: str = Field(description="Артикул / уникальный ID товара")
    title: str = Field(description="Наименование товара")
    price: float = Field(description="Цена в числовом формате")
    old_price: Optional[float] = Field(default=None, description="Старая цена")
    in_stock: bool = Field(default=True, description="Статус наличия")
    url: str = Field(description="Прямая ссылка на карточку товара")
    category: str = Field(default="Общая", description="Категория каталога")
    scraped_at: str = Field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        cleaned = re.sub(r"\s+", " ", v).strip()
        if not cleaned:
            raise ValueError("Название товара не может быть пустым")
        return cleaned

    @field_validator("price", mode="before")
    @classmethod
    def parse_price(cls, v: Any) -> float:
        if isinstance(v, (int, float)):
            return float(v)
        raw_price = re.sub(r"[^\d.,]", "", str(v)).replace(",", ".")
        try:
            return float(raw_price)
        except ValueError:
            return 0.0

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned.startswith(("http://", "https://")):
            raise ValueError(f"Недопустимая схема URL (разрешены только http/https): {cleaned}")
        if not is_safe_public_url(cleaned):
            raise ValueError(f"Заблокирован приватный/loopback хост (SSRF Guard): {cleaned}")
        return cleaned


# ==============================================================================
# 2. МЕНЕДЖЕР ПРОКСИ (Proxy Pool & Rotation)
# ==============================================================================

class ProxyManager:
    """Менеджер циклической ротации прокси-серверов."""

    def __init__(self, proxy_list: Optional[List[str]] = None) -> None:
        self.proxies: List[str] = proxy_list or []
        self._cycle = itertools.cycle(self.proxies) if self.proxies else None

    @classmethod
    def from_file(cls, filepath: Optional[str]) -> ProxyManager:
        if not filepath or not Path(filepath).exists():
            return cls([])
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        return cls(lines)

    def get_next(self) -> Optional[Dict[str, str]]:
        """Возвращает конфигурацию прокси для контекста Playwright."""
        if not self._cycle:
            return None
        raw_proxy = next(self._cycle)
        config: Dict[str, str] = {"server": raw_proxy}
        if "://" in raw_proxy and "@" in raw_proxy:
            scheme, rest = raw_proxy.split("://", 1)
            # rsplit гарантирует корректное разбиение даже при символе @ в пароле
            creds, addr = rest.rsplit("@", 1)
            if ":" in creds:
                username, password = creds.split(":", 1)
                config = {
                    "server": f"{scheme}://{addr}",
                    "username": username,
                    "password": password,
                }
        return config


# ==============================================================================
# 3. АСИНХРОННОЕ ХРАНИЛИЩЕ БАЗЫ ДАННЫХ (SQLite Standard + Thread Pool)
# ==============================================================================

class DatabaseStorage:
    """Асинхронное хранилище SQLite на базе встроенного модуля sqlite3."""

    def __init__(self, db_path: str = "scraped_data.db") -> None:
        self.db_path = db_path

    def _init_db_sync(self) -> None:
        db_path = Path(self.db_path)
        if db_path.parent:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS products (
                    sku TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT,
                    price REAL,
                    old_price REAL,
                    in_stock INTEGER,
                    url TEXT,
                    scraped_at TEXT
                )
                """
            )
            conn.commit()

    async def init_db(self) -> None:
        await asyncio.to_thread(self._init_db_sync)

    def _save_items_sync(self, items: List[ProductItem]) -> int:
        inserted = 0
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            for item in items:
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO products 
                    (sku, title, category, price, old_price, in_stock, url, scraped_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.sku,
                        item.title,
                        item.category,
                        item.price,
                        item.old_price,
                        1 if item.in_stock else 0,
                        item.url,
                        item.scraped_at,
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
            conn.commit()
        return inserted

    async def save_items(self, items: List[ProductItem]) -> int:
        """Неблокирующее сохранение пачки товаров с защитой от дублей."""
        if not items:
            return 0
        return await asyncio.to_thread(self._save_items_sync, items)


# ==============================================================================
# 4. АБСТРАКТНЫЙ АДАПТЕР ПЛОЩАДКИ (Защита от копипаста)
# ==============================================================================

class BaseCatalogAdapter(ABC):
    """Интерфейс целевого сайта. Реализуется индивидуально под заказчика."""

    @property
    @abstractmethod
    def target_name(self) -> str:
        pass

    @abstractmethod
    async def build_catalog_url(self, page_number: int) -> str:
        pass

    @abstractmethod
    async def extract_items(self, page: Page) -> List[ProductItem]:
        pass


class DemoBooksAdapter(BaseCatalogAdapter):
    """Демонстрационный адаптер для открытого тестового стенда."""

    @property
    def target_name(self) -> str:
        return "Books_Sandbox_Demonstration"

    async def build_catalog_url(self, page_number: int) -> str:
        return f"https://books.toscrape.com/catalogue/page-{page_number}.html"

    async def extract_items(self, page: Page) -> List[ProductItem]:
        items: List[ProductItem] = []
        cards = await page.query_selector_all("article.product_pod")

        for card in cards:
            title_el = await card.query_selector("h3 a")
            price_el = await card.query_selector("p.price_color")
            stock_el = await card.query_selector("p.instock.availability")
            # title_el и link_el — один и тот же узел; используем title_el напрямую

            if not (title_el and price_el):
                continue

            raw_title = (
                await title_el.get_attribute("title") or await title_el.inner_text()
            )
            raw_price = await price_el.inner_text()
            raw_stock = await stock_el.inner_text() if stock_el else ""
            href = await title_el.get_attribute("href") or ""
            full_url = f"https://books.toscrape.com/catalogue/{href.replace('../', '')}"
            sku_val = f"SKU-{abs(hash(full_url)) % 100000:05d}"

            try:
                item = ProductItem(
                    sku=sku_val,
                    title=raw_title,
                    price=raw_price,  # type: ignore[arg-type]  # парсится field_validator
                    in_stock="In stock" in raw_stock,
                    url=full_url,
                    category="Литература / Каталог",
                )
                items.append(item)
            except Exception as exc:
                console.print(
                    f"[yellow][ВАЛИДАЦИЯ][/yellow] Пропущена карточка '{raw_title}': {exc}"
                )

        return items


# ==============================================================================
# 5. ОСНОВНОЕ СКАНИРУЮЩЕЕ ЯДРО (Scraping Engine Core)
# ==============================================================================

class ScrapingEngine:
    def __init__(
        self,
        adapter: BaseCatalogAdapter,
        max_concurrency: int = 3,
        headless: bool = True,
        proxy_manager: Optional[ProxyManager] = None,
        db_storage: Optional[DatabaseStorage] = None,
        max_response_size: int = 15 * 1024 * 1024,
        max_redirects: int = 5,
    ) -> None:
        self.adapter = adapter
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.headless = headless
        self.proxy_manager = proxy_manager or ProxyManager()
        self.db = db_storage
        self.max_response_size = max_response_size
        self.max_redirects = max_redirects
        self.visited_urls: set[str] = set()
        self.collected_items: List[ProductItem] = []

    async def _setup_stealth_context(self, browser: Browser) -> BrowserContext:
        proxy_config = self.proxy_manager.get_next()
        context_kwargs: Dict[str, Any] = {
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "locale": "ru-RU",
            "timezone_id": "Asia/Yekaterinburg",
        }
        if proxy_config:
            context_kwargs["proxy"] = proxy_config

        context = await browser.new_context(**context_kwargs)

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            """
        )

        async def intercept_route(route: Route) -> None:
            """Блокирует тяжёлые ресурсы для ускорения и экономии трафика прокси."""
            if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", intercept_route)
        return context

    async def _human_scroll(self, page: Page) -> None:
        for _ in range(3):
            scroll_delta = random.randint(350, 650)
            await page.mouse.wheel(0, scroll_delta)
            await asyncio.sleep(random.uniform(0.3, 0.6))

    async def scrape_page(self, context: BrowserContext, page_num: int) -> None:
        async with self.semaphore:
            target_url = await self.adapter.build_catalog_url(page_num)
            if not is_safe_public_url(target_url):
                console.print(f"[bold red][SSRF BLOCKED][/bold red] Заблокирован небезопасный URL: {target_url}")
                return

            if target_url in self.visited_urls:
                console.print(f"[yellow][КРАУЛЕР][/yellow] Пропуск уже посещенного URL (защита от циклов): {target_url}")
                return
            self.visited_urls.add(target_url)

            page = await context.new_page()

            try:
                console.print(f"[cyan][СЕТЬ][/cyan] Стр. {page_num}: {target_url}")
                response = await page.goto(
                    target_url, timeout=30000, wait_until="domcontentloaded"
                )

                if not response or response.status >= 400:
                    status_code = response.status if response else "ERR"
                    console.print(
                        f"[bold red][ОШИБКА][/bold red] HTTP {status_code} на стр. {page_num}"
                    )
                    return

                # Проверка лимита размера ответа (защита от Memory DoS / Decompression Bomb)
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self.max_response_size:
                    console.print(
                        f"[bold red][DOS BLOCKED][/bold red] Превышен max_response_size: {content_length} байт"
                    )
                    return

                await self._human_scroll(page)
                items = await self.adapter.extract_items(page)

                new_db_records = 0
                if self.db:
                    new_db_records = await self.db.save_items(items)

                self.collected_items.extend(items)
                console.print(
                    f"[green][УСПЕХ][/green] Стр. {page_num} обработана | "
                    f"Собрано: {len(items)} | Новых в БД: {new_db_records} | "
                    f"Всего в ОЗУ: {len(self.collected_items)}"
                )

                await asyncio.sleep(random.uniform(0.8, 2.0))

            except Exception as e:
                console.print(
                    f"[bold red][СБОЙ][/bold red] Ошибка парсинга стр. {page_num}: {e}"
                )
            finally:
                await page.close()

    async def run(self, total_pages: int = 3) -> List[ProductItem]:
        console.print(
            f"\n[bold yellow]>>> Запуск скрапинг-ядра: {self.adapter.target_name}[/bold yellow]"
        )
        if self.db:
            await self.db.init_db()
            console.print(
                f"[dim cyan][БД][/dim cyan] SQLite хранилище подключено: {self.db.db_path}"
            )

        start_time = time.perf_counter()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            context = await self._setup_stealth_context(browser)

            tasks = [
                self.scrape_page(context, page_num)
                for page_num in range(1, total_pages + 1)
            ]
            await asyncio.gather(*tasks)

            await context.close()
            await browser.close()

        elapsed = time.perf_counter() - start_time
        console.print(
            f"[bold green]>>> Завершено за {elapsed:.2f} сек. "
            f"Итого позиций: {len(self.collected_items)}[/bold green]\n"
        )
        return self.collected_items


# ==============================================================================
# 6. ГЕНЕРАТОР ОТЧЕТОВ EXCEL
# ==============================================================================

class ExcelReportExporter:
    @staticmethod
    def export(
        items: List[ProductItem], output_filepath: str = "catalog_report.xlsx"
    ) -> Optional[Path]:
        wb = Workbook()
        # wb.active не бывает None для новой книги, но mypy требует явного приведения
        ws: Worksheet = wb.active  # type: ignore[assignment]
        ws.title = "Выгрузка данных"

        headers = [
            "Артикул (SKU)",
            "Наименование",
            "Категория",
            "Цена (RUB)",
            "Наличие",
            "Дата фиксации",
            "URL",
        ]
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_fill = PatternFill(
            start_color="1F497D", end_color="1F497D", fill_type="solid"
        )
        thin_border = Border(
            left=Side(style="thin", color="E0E0E0"),
            right=Side(style="thin", color="E0E0E0"),
            top=Side(style="thin", color="E0E0E0"),
            bottom=Side(style="thin", color="E0E0E0"),
        )

        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")

        def _shield_cell(value: Any) -> Any:
            """Нейтрализует CSV/Excel Formula Injection (CWE-1236)."""
            if isinstance(value, str):
                if value.strip().startswith(("=", "+", "-", "@", "\t", "\r")):
                    return f"'{value}"
            return value

        for row_idx, item in enumerate(items, start=2):
            ws.append(
                [
                    _shield_cell(item.sku),
                    _shield_cell(item.title),
                    _shield_cell(item.category),
                    item.price,
                    "В наличии" if item.in_stock else "Под заказ",
                    item.scraped_at,
                    _shield_cell(item.url),
                ]
            )
            ws.cell(row=row_idx, column=4).number_format = "#,##0.00"
            for col_idx in range(1, len(headers) + 1):
                ws.cell(row=row_idx, column=col_idx).border = thin_border

        # Итерируем только по реально заполненным столбцам (по числу заголовков)
        for col_idx in range(1, len(headers) + 1):
            col_cells = [
                ws.cell(row=r, column=col_idx) for r in range(1, len(items) + 2)
            ]
            max_len = max(len(str(cell.value or "")) for cell in col_cells)
            col_letter = get_column_letter(col_idx)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 50)

        target = Path(output_filepath)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            wb.save(target)
            console.print(
                f"[bold cyan][ЭКСПОРТ EXCEL][/bold cyan] Файл сохранен: {target.resolve()}"
            )
        except OSError as exc:
            console.print(
                f"[bold red][ОШИБКА ЭКСПОРТА][/bold red] Не удалось сохранить файл: {exc}"
            )
            return None
        return target


class CSVReportExporter:
    """Экспортер в CSV (Standard Library, Zero-Pip) с кодировкой UTF-8 BOM для Microsoft Excel."""

    @staticmethod
    def export(
        items: List[ProductItem], output_filepath: str = "catalog_report.csv"
    ) -> Optional[Path]:
        def _shield_cell(value: Any) -> Any:
            if isinstance(value, str):
                s = value.strip()
                if s.startswith(("=", "+", "-", "@", "\t", "\r")):
                    return f"'{value}"
            return value

        target = Path(output_filepath)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(
                    [
                        "Артикул (SKU)",
                        "Наименование",
                        "Категория",
                        "Цена (RUB)",
                        "Наличие",
                        "Дата фиксации",
                        "URL",
                    ]
                )
                for item in items:
                    writer.writerow(
                        [
                            _shield_cell(item.sku),
                            _shield_cell(item.title),
                            _shield_cell(item.category),
                            f"{item.price:.2f}",
                            "В наличии" if item.in_stock else "Под заказ",
                            item.scraped_at,
                            _shield_cell(item.url),
                        ]
                    )
            console.print(
                f"[bold cyan][ЭКСПОРТ CSV][/bold cyan] Файл сохранен: {target.resolve()}"
            )
            return target
        except OSError as exc:
            console.print(
                f"[bold red][ОШИБКА ЭКСПОРТА][/bold red] Не удалось сохранить CSV файл: {exc}"
            )
            return None


def generate_demo_catalog(pages: int = 3, items_per_page: int = 5) -> List[ProductItem]:
    """Генерирует синтетический каталог товаров для офлайн/демо режима (Zero-Pip)."""
    demo_titles = [
        ("Clean Architecture: A Craftsman's Guide to Software Structure", "Engineering", 3800.0, True),
        ("Designing Data-Intensive Applications", "Distributed Systems", 4500.0, True),
        ("Fluent Python: Clear, Concise, and Effective Programming", "Python", 3950.0, True),
        ("Site Reliability Engineering: How Google Runs Production Systems", "DevOps", 3200.0, True),
        ("Building Microservices: Designing Fine-Grained Systems", "Architecture", 3600.0, True),
        ("Database Internals: A Deep Dive into How Distributed Systems Work", "Databases", 4100.0, False),
        ("Refactoring: Improving the Design of Existing Code", "Refactoring", 3400.0, True),
        ("Release It!: Design and Deploy Production-Ready Software", "Reliability", 2900.0, True),
        ("Domain-Driven Design: Tackling Complexity in the Heart of Software", "Architecture", 4800.0, True),
        ("Operating Systems: Three Easy Pieces", "Systems", 2700.0, True),
    ]
    items: List[ProductItem] = []
    idx = 1
    for page in range(1, pages + 1):
        for item_idx in range(items_per_page):
            sample = demo_titles[(idx - 1) % len(demo_titles)]
            sku = f"BOOK-DEMO-{idx:04d}"
            title = f"{sample[0]} (Vol. {page})" if page > 1 else sample[0]
            price = sample[2] + (page - 1) * 150.0
            category = sample[1]
            in_stock = sample[3]
            url = f"https://books.toscrape.com/catalogue/demo-item-{idx}/index.html"
            items.append(
                ProductItem(
                    sku=sku,
                    title=title,
                    category=category,
                    price=price,
                    old_price=price * 1.15,
                    in_stock=in_stock,
                    url=url,
                    scraped_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
            idx += 1
    return items


# ==============================================================================
# 7. CLI ТОЧКА ВХОДА (Аргументы командной строки)
# ==============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enterprise Web Scraper & Catalog Extractor CLI Engine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-p", "--pages", type=int, default=3, help="Количество страниц каталога для сбора"
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=3,
        help="Параллелизм (максимум одновременных вкладок)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default="demo_catalog.xlsx",
        help="Имя выходного Excel/CSV файла",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="scraped_data.db",
        help="Путь к файлу SQLite базы данных (пустая строка '' — отключить БД)",
    )
    parser.add_argument(
        "--proxies",
        type=str,
        default=None,
        help="Путь к текстовому файлу со списком прокси",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Запуск с видимым окном браузера (для отладки)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Автономный демонстрационный режим (Zero-Pip, без внешних запросов к сети)",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # Офлайн демонстрационный режим (Zero-Pip)
    if args.demo:
        console.print("[bold yellow]>>> Запуск в автономном DEMO-режиме (Zero-Pip)...[/bold yellow]")
        demo_items = generate_demo_catalog(pages=args.pages)
        if args.db and args.db.strip():
            db_storage = DatabaseStorage(args.db)
            await db_storage.init_db()
            inserted = await db_storage.save_items(demo_items)
            console.print(f"[dim cyan][БД][/dim cyan] Сохранено демо-записей: {inserted}")

        output_path = Path(args.output)
        if output_path.suffix.lower() == ".csv" or not HAS_OPENPYXL:
            CSVReportExporter.export(demo_items, str(output_path))
        else:
            ExcelReportExporter.export(demo_items, str(output_path))
        console.print(
            f"[bold green]>>> Демо-сессия успешно завершена. Сгенерировано позиций: {len(demo_items)}[/bold green]\n"
        )
        return

    missing = []
    if not HAS_PLAYWRIGHT:
        missing.append("playwright")
    if not HAS_PYDANTIC:
        missing.append("pydantic")
    if not HAS_OPENPYXL:
        missing.append("openpyxl")
    if missing:
        print("\n" + "=" * 65)
        print("⚠️  Enterprise Web Scraper: Требуются внешние библиотеки")
        print("=" * 65)
        print(f"Не установлены модули: {', '.join(missing)}")
        print("\nДля запуска в автономном режиме без pip используйте:")
        print("   python main.py --demo\n")
        print("Для установки всех зависимостей выполните:")
        print("   pip install -r requirements.txt")
        print("   playwright install chromium\n")
        print("=" * 65 + "\n")
        sys.exit(1)

    adapter = DemoBooksAdapter()

    if Table is not None:
        table = Table(
            title="Параметры сессии Enterprise Scraper", border_style="bright_blue"
        )
        table.add_column("Параметр", style="cyan")
        table.add_column("Значение", style="green")
        table.add_row("Целевой адаптер", adapter.target_name)
        table.add_row("Количество страниц", str(args.pages))
        table.add_row("Параллельные вкладки", str(args.concurrency))
        table.add_row(
            "Режим отображения",
            "Headful (С окном)" if args.headful else "Headless (Скрытый)",
        )
        table.add_row("Выходной Excel", args.output)
        table.add_row("SQLite база данных", args.db)
        table.add_row(
            "Файл прокси",
            args.proxies if args.proxies else "Не используется (Прямое подключение)",
        )
        console.print(table)
    else:
        console.print(f"[Параметры сессии Scraper] Адаптер: {adapter.target_name} | Страниц: {args.pages} | Параллелизм: {args.concurrency}")

    proxy_mgr = ProxyManager.from_file(args.proxies)
    # Пустая строка в --db означает «не использовать БД»
    db_storage = DatabaseStorage(args.db) if args.db and args.db.strip() else None

    engine = ScrapingEngine(
        adapter=adapter,
        max_concurrency=args.concurrency,
        headless=not args.headful,
        proxy_manager=proxy_mgr,
        db_storage=db_storage,
    )

    data = await engine.run(total_pages=args.pages)
    if data:
        if Path(args.output).suffix.lower() == ".csv":
            CSVReportExporter.export(data, args.output)
        else:
            ExcelReportExporter.export(data, args.output)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[!] Сессия прервана пользователем (SIGINT / Ctrl+C). Завершение работы...")
        sys.exit(130)