"""FastAPI app (M0 skeleton: health check and static files; the full API lands in M3)."""
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from core.settings import get_settings

STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="GoldenHour")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "llm_mode": get_settings().llm_mode}
