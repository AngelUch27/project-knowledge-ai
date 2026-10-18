from fastapi import FastAPI

app = FastAPI(
    title="Project Knowledge AI API",
    description="Backend inicial para el demo Project Knowledge AI.",
    version="0.1.0",
)


@app.get("/health")
def health() -> dict[str, str]:
    """Return a simple health status without external service checks."""
    return {"status": "ok", "service": "project-knowledge-ai-api"}
