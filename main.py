"""
Enterprise Async Web Scraper & Catalog Extractor (Full Suite)
Архитектурный шаблон промышленного парсера:
- CLI управление флагами запуска (argparse)
- Неблокирующее сохранение в SQLite (sqlite3 + asyncio.to_thread) с защитой от сбоев
- Пул ротации прокси (HTTP/SOCKS5)
- Stealth-эмуляция человека и фильтрация медиа-трафика (Playwright)
- Строгая схема Pydantic v2
- Генерация отчетов Excel с авто-стилями (OpenPyXL)
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import random
import re
import sqlite3
import time
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright
from pydantic import BaseModel, Field, field_validator
from rich.console import Console
from rich.table import Table

console = Console()

# Типы медиа-ресурсов, блокируемых для экономии трафика и ускорения сбора
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})


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
            raise ValueError(f"Некорректный формат URL: {cleaned}")
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
    ) -> None:
        self.adapter = adapter
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.headless = headless
        self.proxy_manager = proxy_manager or ProxyManager()
        self.db = db_storage
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

        for row_idx, item in enumerate(items, start=2):
            ws.append(
                [
                    item.sku,
                    item.title,
                    item.category,
                    item.price,
                    "В наличии" if item.in_stock else "Под заказ",
                    item.scraped_at,
                    item.url,
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


# ==============================================================================
# 7. CLI ТОЧКА ВХОДА (Аргументы командной строки)
# ==============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enterprise Web Scraper & Catalog Extractor CLI Engine",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--pages", type=int, default=3, help="Количество страниц каталога для сбора"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Параллелизм (максимум одновременных вкладок)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="demo_catalog.xlsx",
        help="Имя выходного Excel файла",
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
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    adapter = DemoBooksAdapter()

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
        ExcelReportExporter.export(data, args.output)


if __name__ == "__main__":
    asyncio.run(main())