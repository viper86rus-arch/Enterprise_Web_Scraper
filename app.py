"""
Enterprise Web Scraper — FastAPI Web Shell
Веб-оболочка поверх main.py с:
- Реал-тайм логами через WebSocket
- Фоновым запуском ScrapingEngine
- Скачиванием Excel-отчёта
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import Request
from pydantic import BaseModel

# --------------------------------------------------------------------------- #
# Импорт компонентов движка без запуска CLI                                    #
# --------------------------------------------------------------------------- #
from main import (
    DatabaseStorage,
    DemoBooksAdapter,
    ExcelReportExporter,
    ProxyManager,
    ScrapingEngine,
)

# --------------------------------------------------------------------------- #
# Глобальное состояние задачи (один скрапер за раз)                            #
# --------------------------------------------------------------------------- #
class TaskState:
    running: bool = False
    output_file: Optional[str] = None
    log_queue: asyncio.Queue = asyncio.Queue()

state = TaskState()

# --------------------------------------------------------------------------- #
# Перехватчик вывода Rich / print → WebSocket-очередь                          #
# --------------------------------------------------------------------------- #
_ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

class WebSocketLogger(io.TextIOBase):
    """Перенаправляет print/stdout в asyncio-очередь для трансляции в WS."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    def write(self, text: str) -> int:
        clean = _ANSI_ESCAPE.sub("", text)
        # Убираем Rich-теги типа [cyan], [bold red], etc.
        clean = re.sub(r"\[/?[^\]]*\]", "", clean)
        clean = clean.strip()
        if clean:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(self._queue.put_nowait, clean)
                else:
                    self._queue.put_nowait(clean)
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# Патч Rich Console так, чтобы он тоже слал в очередь                         #
# --------------------------------------------------------------------------- #
import main as _main_module
from rich.console import Console

def _make_ws_console(queue: asyncio.Queue) -> Console:
    """Создаёт Rich Console, который пишет в WebSocketLogger."""
    ws_logger = WebSocketLogger(queue)
    return Console(file=ws_logger, highlight=False, markup=True)


# --------------------------------------------------------------------------- #
# Параметры запуска из веб-формы                                               #
# --------------------------------------------------------------------------- #
class ScrapeRequest(BaseModel):
    pages: int = 3
    concurrency: int = 3
    output: str = "catalog_report.xlsx"
    db: str = "scraped_data.db"


# --------------------------------------------------------------------------- #
# FastAPI приложение                                                            #
# --------------------------------------------------------------------------- #
app = FastAPI(title="Enterprise Web Scraper UI")

_HTML_PATH = Path("templates/index.html")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=_HTML_PATH.read_text(encoding="utf-8"))


@app.post("/api/run")
async def run_scraper(params: ScrapeRequest):
    """Запускает скрапер в фоне, возвращает статус немедленно."""
    if state.running:
        return {"status": "already_running", "message": "Задача уже выполняется"}

    # Очищаем очередь от прошлого прогона
    while not state.log_queue.empty():
        try:
            state.log_queue.get_nowait()
        except asyncio.QueueEmpty:
            break

    state.running = True
    state.output_file = None

    asyncio.create_task(_run_scrape_task(params))
    return {"status": "started", "message": "Скрапер запущен в фоне"}


@app.get("/api/status")
async def get_status():
    return {
        "running": state.running,
        "output_file": state.output_file,
        "ready": state.output_file is not None and Path(state.output_file).exists(),
    }


@app.get("/api/download")
async def download_report():
    if not state.output_file or not Path(state.output_file).exists():
        return {"error": "Файл не найден. Сначала запустите скрапер."}
    return FileResponse(
        path=state.output_file,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=Path(state.output_file).name,
    )


# --------------------------------------------------------------------------- #
# WebSocket: трансляция логов в реальном времени                                #
# --------------------------------------------------------------------------- #
@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            try:
                # Ждём сообщения с таймаутом, чтобы не блокировать навсегда
                message = await asyncio.wait_for(state.log_queue.get(), timeout=1.0)
                payload = json.dumps({"type": "log", "text": message})
                await websocket.send_text(payload)
            except asyncio.TimeoutError:
                # Отправляем ping чтобы клиент знал — соединение живо
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    break
            except WebSocketDisconnect:
                break
    except WebSocketDisconnect:
        pass


# --------------------------------------------------------------------------- #
# Фоновая задача скрапинга                                                     #
# --------------------------------------------------------------------------- #
async def _run_scrape_task(params: ScrapeRequest) -> None:
    """Выполняет полный цикл скрапинга и пишет логи в state.log_queue."""
    queue = state.log_queue

    async def log(msg: str) -> None:
        await queue.put(msg)

    try:
        await log(f"Старт задачи | Страниц: {params.pages} | "
                  f"Параллелизм: {params.concurrency} | Файл: {params.output}")

        # Патчим console в main, чтобы он писал в нашу очередь
        ws_console = _make_ws_console(queue)
        _main_module.console = ws_console

        adapter = DemoBooksAdapter()
        proxy_mgr = ProxyManager()
        db_storage = DatabaseStorage(params.db) if params.db.strip() else None

        engine = ScrapingEngine(
            adapter=adapter,
            max_concurrency=params.concurrency,
            headless=True,
            proxy_manager=proxy_mgr,
            db_storage=db_storage,
        )

        await log(f"[БД] SQLite: {params.db or 'отключено'}")
        await log(f"[СЕТЬ] Целевой адаптер: {adapter.target_name}")

        data = await engine.run(total_pages=params.pages)

        if data:
            output_path = params.output
            result = await asyncio.to_thread(
                ExcelReportExporter.export, data, output_path
            )
            if result:
                state.output_file = str(result)
                await log(f"[УСПЕХ] Excel-отчёт готов: {result.name} ({len(data)} записей)")
            else:
                await log("[ОШИБКА] Не удалось сохранить Excel-файл")
        else:
            await log("[ПРЕДУПРЕЖДЕНИЕ] Данные не собраны — файл не создан")

        await log("ЗАДАЧА ЗАВЕРШЕНА")

    except Exception as exc:
        await log(f"[СБОЙ] Критическая ошибка: {exc}")
    finally:
        state.running = False
        # Восстанавливаем стандартный console в main
        _main_module.console = Console()
        # Финальный маркер для клиента
        await queue.put("__DONE__")


# --------------------------------------------------------------------------- #
# Точка входа                                                                  #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
