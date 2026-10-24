"""Entry point: `python run.py` reads LLM_MODE and PORT from .env and starts uvicorn."""
import uvicorn

from core.settings import get_settings


def main() -> None:
    settings = get_settings()
    print(f"GoldenHour -> http://{settings.host}:{settings.port}  (LLM_MODE={settings.llm_mode})", flush=True)
    if settings.llm_mode == "groq" and not settings.groq_api_key:
        print("WARNING: LLM_MODE=groq but GROQ_API_KEY is empty. Unless a proxy injects the key, every model call "
              "will fail and cases will be routed to the human queue. Set GROQ_API_KEY or use LLM_MODE=replay.", flush=True)
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
