"""
api/main.py
-----------
FastAPI application for ZeroClick.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

from agents.orchestrator import (
    run_once,
    get_inbox_state,
    generate_brd_from_thread,
)


# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background poller on startup
    task = asyncio.create_task(_poll_loop())
    yield
    # Cancel poller on shutdown
    task.cancel()


app = FastAPI(title="ZeroClick API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Background poller ─────────────────────────────────────────────────────────

_poller_running = False

async def _poll_loop():
    """
    Runs every 30 seconds in the background.
    Uses asyncio.to_thread() so blocking IMAP/Gemini/SMTP calls
    don't freeze the FastAPI event loop.
    """
    global _poller_running
    _poller_running = True
    print("[Poller] Started — checking inbox every 30 seconds")
    while True:
        try:
            results = await asyncio.to_thread(run_once)
            if results:
                print(f"[Poller] Processed {len(results)} new email(s)")
        except Exception as e:
            print(f"[Poller] Error: {e}")
        await asyncio.sleep(30)


# ── Request models ────────────────────────────────────────────────────────────

class BRDRequest(BaseModel):
    thread: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "app": "ZeroClick"}


@app.get("/inbox")
def get_inbox():
    return {"emails": get_inbox_state()}


@app.post("/poll")
async def trigger_poll():
    """Manually trigger an inbox poll without blocking the server."""
    try:
        results = await asyncio.to_thread(run_once)
        return {
            "message": f"Processed {len(results)} new email(s)",
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/brd")
async def generate_brd(request: BRDRequest):
    if not request.thread.strip():
        raise HTTPException(status_code=400, detail="Thread text cannot be empty")
    try:
        brd = await asyncio.to_thread(generate_brd_from_thread, request.thread)
        return {"brd": brd}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/meetings")
def get_meetings():
    all_emails = get_inbox_state()
    meetings = [e for e in all_emails if e.get("status") == "scheduled"]
    return {"meetings": meetings}


@app.get("/status")
def get_status():
    all_emails = get_inbox_state()
    return {
        "poller_running" : _poller_running,
        "total_processed": len(all_emails),
        "scheduled"      : len([e for e in all_emails if e.get("status") == "scheduled"]),
        "clarifications" : len([e for e in all_emails if e.get("status") == "clarification_sent"]),
        "skipped"        : len([e for e in all_emails if e.get("status") == "skipped"]),
    }