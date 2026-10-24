"""Entry point: `python run.py` reads LLM_MODE and PORT from .env and starts uvicorn."""
import uvicorn

from core.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"GoldenHour -> http://{settings.host}:{settings.port}  (LLM_MODE={settings.llm_mode})")
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
