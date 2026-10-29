from datetime import datetime, timezone


def api_error(code: str, detail: str, **extra) -> dict:
    result = {
        "error": code,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    result.update(extra)
    return result
