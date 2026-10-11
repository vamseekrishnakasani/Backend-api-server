"""
Module 1: a small, production-shaped FastAPI backend for Azure App Service.

Design choices worth noticing (we'll go deeper in later modules):
  - Versioned routes (/api/v1/...) so v2 can ship without breaking v1 clients.
  - Separate liveness (dyz) probe/healthz) and readiness (/reas.
  - Every request gets an X-Request-ID and one structured JSON log line.
    App Service captures stdout, so these show up in Log Stream as-is.
  - Correct status codes: 201 on create, 404 on miss, 204 on delete.
"""
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

# ---------- Logging: one JSON object per line to stdout ----------
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        return json.dumps(payload)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())
log = logging.getLogger("api")
log.setLevel(os.getenv("LOG_LEVEL", "INFO"))
log.addHandler(handler)
log.propagate = False

# ---------- App lifecycle ----------
STATE = {"ready": False}

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: open DB pools, warm caches, load models, etc.
    STATE["ready"] = True
    log.info("startup complete", extra={"extra_fields": {"version": APP_VERSION}})
    yield
    # Shutdown: App Service sends SIGTERM on restart/swap; drain cleanly here.
    STATE["ready"] = False
    log.info("shutdown complete")

APP_VERSION = os.getenv("APP_VERSION", "1.0.0")
app = FastAPI(title="Items API", version=APP_VERSION, lifespan=lifespan)

# ---------- Middleware: request id + access log ----------
@app.middleware("http")
async def request_context(request: Request, call_next):
    rid = request.headers.get("x-request-id", str(uuid.uuid4()))
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["x-request-id"] = rid
    log.info(
        "request",
        extra={"extra_fields": {
            "request_id": rid,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        }},
    )
    return response

# ---------- Health ----------
@app.get("/healthz", tags=["health"])
def liveness():
    """Process is up. Keep this dependency-free."""
    return {"status": "ok"}

@app.get("/readyz", tags=["health"])
def readiness(response: Response):
    """Ready to take traffic. Later: check DB/cache/downstreams here."""
    if not STATE["ready"]:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting"}
    return {"status": "ready", "version": APP_VERSION}

# ---------- Domain: Items (in-memory for now; DB comes later) ----------
class ItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price: float = Field(gt=0)
    description: Optional[str] = None

class Item(ItemIn):
    id: str

DB: dict[str, Item] = {}

@app.get("/api/v1/items", response_model=list[Item], tags=["items"])
def list_items(limit: int = 50, offset: int = 0):
    return list(DB.values())[offset : offset + limit]

@app.get("/api/v1/items/{item_id}", response_model=Item, tags=["items"])
def get_item(item_id: str):
    if item_id not in DB:
        raise HTTPException(status_code=404, detail="item not found")
    return DB[item_id]

@app.post("/api/v1/items", response_model=Item, status_code=201, tags=["items"])
def create_item(body: ItemIn, response: Response):
    item = Item(id=str(uuid.uuid4()), **body.model_dump())
    DB[item.id] = item
    response.headers["Location"] = f"/api/v1/items/{item.id}"
    return item

@app.delete("/api/v1/items/{item_id}", status_code=204, tags=["items"])
def delete_item(item_id: str):
    if DB.pop(item_id, None) is None:
        raise HTTPException(status_code=404, detail="item not found")
    return Response(status_code=204)