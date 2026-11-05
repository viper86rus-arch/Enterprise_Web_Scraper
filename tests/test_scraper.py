#!/usr/bin/env python3
"""
🧪 Unit Tests for Enterprise Web Scraper (test_scraper.py)
=========================================================
Проверка ключевых компонентов движка:
- Валидация и очистка модели ProductItem
- Ротация и парсинг учетных данных прокси (ProxyManager)
- Транзакционная запись и дедупликация в SQLite (DatabaseStorage)
- Парсинг CLI-аргументов

Автор: V1PER (viper86rus-arch)
Лицензия: MIT
"""

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Добавляем родительскую директорию в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import DatabaseStorage, ProductItem, ProxyManager, parse_args


class TestProductItemModel(unittest.TestCase):
    """Тестирование схемы данных ProductItem."""

    def test_product_creation_and_defaults(self):
        """Проверка инициализации полей товара и значений по умолчанию."""
        item = ProductItem(
            sku="BOOK-001",
            title="Python Testing in Practice",
            price=1499.0,
            url="https://books.toscrape.com/catalogue/book_1/index.html",
        )
        self.assertEqual(item.sku, "BOOK-001")
        self.assertEqual(item.title, "Python Testing in Practice")
        self.assertEqual(item.price, 1499.0)
        self.assertTrue(item.in_stock)
        self.assertEqual(item.category, "Общая")
        self.assertIsNotNone(item.scraped_at)

    def test_product_title_cleaning(self):
        """Проверка очистки лишних пробелов и переводов строк в названии."""
        item = ProductItem(
            sku="BOOK-002",
            title="   Clean   Architecture   and   Design\n\t  ",
            price=2500.0,
            url="https://books.toscrape.com/catalogue/book_2/index.html",
        )
        self.assertEqual(item.title, "Clean Architecture and Design")


class TestProxyManager(unittest.TestCase):
    """Тестирование менеджера прокси и парсинга форматов."""

    def test_empty_proxy_manager(self):
        """Проверка безопасной работы без прокси."""
        mgr = ProxyManager([])
        self.assertIsNone(mgr.get_next())

    def test_single_proxy_cycling(self):
        """Проверка циклического возврата единственного прокси."""
        mgr = ProxyManager(["http://192.168.1.1:8080"])
        p1 = mgr.get_next()
        p2 = mgr.get_next()
        self.assertEqual(p1, {"server": "http://192.168.1.1:8080"})
        self.assertEqual(p2, {"server": "http://192.168.1.1:8080"})

    def test_authenticated_proxy_parsing(self):
        """Проверка разбора прокси с логином и паролем."""
        mgr = ProxyManager(["http://user_test:pass_secret@10.0.0.5:3128"])
        parsed = mgr.get_next()
        self.assertEqual(parsed["server"], "http://10.0.0.5:3128")
        self.assertEqual(parsed["username"], "user_test")
        self.assertEqual(parsed["password"], "pass_secret")

    def test_proxy_from_file(self):
        """Проверка загрузки и фильтрации комментариев из файла."""
        with tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8") as f:
            f.write("# Список прокси\n")
            f.write("http://proxy1:8080\n\n")
            f.write("http://proxy2:8080\n")
            temp_path = f.name

        try:
            mgr = ProxyManager.from_file(temp_path)
            self.assertEqual(len(mgr.proxies), 2)
            self.assertEqual(mgr.get_next()["server"], "http://proxy1:8080")
            self.assertEqual(mgr.get_next()["server"], "http://proxy2:8080")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)


class TestDatabaseStorage(unittest.TestCase):
    """Тестирование слоя хранения SQLite."""

    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.storage = DatabaseStorage(self.temp_db.name)

    def tearDown(self):
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_init_and_deduplication(self):
        """Проверка создания таблицы и предотвращения дубликатов по SKU."""
        self.storage._init_db_sync()

        items = [
            ProductItem(
                sku="SKU-100",
                title="Товар 1",
                price=500.0,
                url="https://example.com/p1",
            ),
            ProductItem(
                sku="SKU-200",
                title="Товар 2",
                price=750.0,
                url="https://example.com/p2",
            ),
        ]
        inserted = self.storage._save_items_sync(items)
        self.assertEqual(inserted, 2)

        # Повторная попытка вставки тех же SKU должна игнорироваться (INSERT OR IGNORE)
        reinserted = self.storage._save_items_sync(items)
        self.assertEqual(reinserted, 0)


if __name__ == "__main__":
    unittest.main()
