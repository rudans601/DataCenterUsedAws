"""데모 앱의 멱등 echo 요청. 같은 요청을 한 번만 재시도한다."""
import time
from datetime import datetime, timezone

import requests

ECHO_URL = "http://127.0.0.1:8080/api/echo"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def echo(message, url=ECHO_URL):
    attempts = []
    for attempt in range(2):
        start = time.monotonic()
        status, verified, retryable = None, False, False
        try:
            response = requests.post(url, data=message.encode("utf-8"), timeout=(1, 3), allow_redirects=False)
            status = response.status_code
            retryable = status in (502, 503, 504, 408)
            if status == 200:
                verified = response.json() == [message]
            detail = f"HTTP {status}" if status != 200 or verified else "응답 본문 불일치"
        except (requests.Timeout, requests.ConnectionError) as exc:
            detail, retryable = type(exc).__name__, True
        except (ValueError, requests.RequestException) as exc:
            detail = type(exc).__name__
        attempts.append({"at": now(), "attempt": attempt + 1, "status": status, "verified": verified,
                         "detail": detail, "ms": round((time.monotonic()-start)*1000)})
        if verified or not retryable or attempt == 1:
            break
        time.sleep(.25)
    outcome = "escalated"
    if verified:
        outcome = "recovered" if len(attempts) > 1 else "healthy"
    return {"outcome": outcome, "attempts": attempts}
