"""HTTP 표본을 로컬 stdout에 기록한다. Kubernetes 자격이나 API가 필요하지 않다."""
import http.client
import json
import os
import time
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def request(echo=False):
    connection = http.client.HTTPConnection("web.demo.svc.cluster.local" if echo else "api.demo.svc.cluster.local", 9898, timeout=1)
    start = time.monotonic()
    try:
        if echo:
            connection.request("POST", "/api/echo", body=b"kubeops-check")
        else:
            connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read()
        verified = response.status == 200
        if echo and verified:
            verified = json.loads(body) == ["kubeops-check"]
        return response.status, verified, round((time.monotonic() - start) * 1000, 2)
    except (OSError, ValueError, http.client.HTTPException):
        return 0, False, round((time.monotonic() - start) * 1000, 2)
    finally:
        connection.close()


while True:
    start = now()
    readings = [request() for _ in range(4)]
    finished = now()
    code, _, ms = request()
    client = {"at": now(), "code": code, "ms": ms}
    attempts = []
    for number in (1, 2):
        status, verified, latency = request(echo=True)
        attempts.append({"at": now(), "attempt": number, "status": status, "verified": verified,
                         "ms": latency, "detail": f"HTTP {status}" if status else "연결 실패"})
        if verified or status not in (0, 408, 502, 503, 504):
            break
        if number == 1:
            time.sleep(0.25)
    outcome = ("healthy" if len(attempts) == 1 else "recovered") if verified else "escalated"
    print(json.dumps({"source": "traffic/http-observer", "target": "demo/api",
                      "cluster": os.environ["CLUSTER_INSTANCE"], "start": start, "at": finished, "client": client, "echo": {"outcome": outcome, "attempts": attempts},
                      "codes": [code for code, _, _ in readings], "latencies_ms": [ms for _, _, ms in readings]}), flush=True)
    time.sleep(2)
