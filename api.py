#!/usr/bin/env python3
"""Authenticated HTTP queue for the Naukri profiles."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status


PROJECT_ROOT = Path(__file__).resolve().parent
DATA_ROOT = Path(os.environ.get("NAUKRI_DATA_ROOT", PROJECT_ROOT)).resolve()
API_STATE_DIR = DATA_ROOT / "data" / "api"
RUNS_FILE = API_STATE_DIR / "runs.json"
LOGS_DIR = API_STATE_DIR / "logs"
PROFILE_CONFIGS = {
    1: Path(os.environ.get("PERSON_1_CONFIG", "config.person2.yaml")),
    2: Path(os.environ.get("PERSON_2_CONFIG", "config.yaml")),
}
PROFILE_ENV_KEYS = (
    "NAUKRI_EMAIL",
    "NAUKRI_PASSWORD",
    "NAUKRI_MOBILE",
    "NAUKRI_EXPERIENCE_YEARS",
    "NAUKRI_TARGET_APPLICATIONS",
    "NAUKRI_MAX_PAGES",
    "NAUKRI_RELOCATION_ANSWER",
    "NAUKRI_START_AVAILABILITY",
    "NAUKRI_NOTICE_PERIOD_ANSWER",
)

queue: asyncio.Queue[str] = asyncio.Queue()
runs: dict[str, dict[str, Any]] = {}
worker_task: asyncio.Task[None] | None = None
active_process: asyncio.subprocess.Process | None = None


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_runs() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(RUNS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_runs() -> None:
    API_STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = RUNS_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(runs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(RUNS_FILE)


def profile_environment(person: int) -> dict[str, str]:
    environment = os.environ.copy()
    for generic_key in PROFILE_ENV_KEYS:
        profile_key = f"PERSON_{person}_{generic_key.removeprefix('NAUKRI_')}"
        profile_value = os.environ.get(profile_key, "").strip()
        if profile_value:
            environment[generic_key] = profile_value
        else:
            environment.pop(generic_key, None)
    environment.update(
        {
            "NAUKRI_NON_INTERACTIVE": "1",
            "NAUKRI_HEADLESS": "1",
            "NAUKRI_BROWSER_CHANNEL": "",
            "PYTHONUNBUFFERED": "1",
            "PYTHON_BIN": sys.executable,
        }
    )
    return environment


def command_for(person: int) -> list[str]:
    config_path = PROFILE_CONFIGS[person]
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    if not config_path.exists():
        raise FileNotFoundError(f"Profile {person} config not found: {config_path}")
    use_windows = os.environ.get("NAUKRI_USE_FRESHNESS_WINDOWS", "1").casefold()
    if use_windows in {"1", "true", "yes"}:
        return [
            "bash",
            str(PROJECT_ROOT / "scripts" / "run_profile_windows.sh"),
            "email",
            str(config_path),
        ]
    return [
        sys.executable,
        str(PROJECT_ROOT / "naukri_bot.py"),
        "--config",
        str(config_path),
        "--login-mode",
        "email",
        "--submit",
        "--non-interactive",
    ]


async def run_worker() -> None:
    global active_process
    while True:
        run_id = await queue.get()
        record = runs.get(run_id)
        if record is None:
            queue.task_done()
            continue
        try:
            record.update(status="running", started_at=timestamp())
            save_runs()
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            log_path = LOGS_DIR / f"{run_id}.log"
            record["log_file"] = str(log_path)
            save_runs()
            with log_path.open("ab") as log:
                active_process = await asyncio.create_subprocess_exec(
                    *command_for(int(record["person"])),
                    cwd=PROJECT_ROOT,
                    env=profile_environment(int(record["person"])),
                    stdout=log,
                    stderr=asyncio.subprocess.STDOUT,
                )
                return_code = await active_process.wait()
            record.update(
                status="succeeded" if return_code == 0 else "failed",
                return_code=return_code,
                finished_at=timestamp(),
            )
        except Exception as exc:
            record.update(status="failed", error=str(exc), finished_at=timestamp())
        finally:
            active_process = None
            save_runs()
            queue.task_done()


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    configured = os.environ.get("API_KEY", "")
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY is not configured",
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


def enqueue(person: int) -> dict[str, Any]:
    for run_id, record in runs.items():
        if int(record.get("person", 0)) == person and record.get("status") in {
            "queued",
            "running",
        }:
            return {"run_id": run_id, **record, "deduplicated": True}
    run_id = uuid.uuid4().hex
    runs[run_id] = {
        "person": person,
        "status": "queued",
        "queued_at": timestamp(),
    }
    save_runs()
    queue.put_nowait(run_id)
    return {"run_id": run_id, **runs[run_id], "deduplicated": False}


@asynccontextmanager
async def lifespan(_: FastAPI):
    global runs, worker_task
    runs = load_runs()
    for record in runs.values():
        if record.get("status") in {"queued", "running"}:
            record.update(
                status="failed",
                error="Web App restarted before this run completed",
                finished_at=timestamp(),
            )
    save_runs()
    worker_task = asyncio.create_task(run_worker())
    try:
        yield
    finally:
        if active_process and active_process.returncode is None:
            active_process.terminate()
        if worker_task:
            worker_task.cancel()


app = FastAPI(
    title="Naukri Bot Trigger API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    current = next(
        (
            {"run_id": run_id, **record}
            for run_id, record in runs.items()
            if record.get("status") == "running"
        ),
        None,
    )
    return {"status": "ok", "queued": queue.qsize(), "current": current}


@app.post("/1", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
async def trigger_person_1() -> dict[str, Any]:
    return enqueue(1)


@app.post("/2", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
async def trigger_person_2() -> dict[str, Any]:
    return enqueue(2)


@app.get("/runs/{run_id}", dependencies=[Depends(require_api_key)])
async def run_status(run_id: str) -> dict[str, Any]:
    record = runs.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run_id": run_id, **record}


@app.get("/runs", dependencies=[Depends(require_api_key)])
async def recent_runs() -> list[dict[str, Any]]:
    ordered = sorted(
        runs.items(), key=lambda item: str(item[1].get("queued_at", "")), reverse=True
    )
    return [{"run_id": run_id, **record} for run_id, record in ordered[:100]]
