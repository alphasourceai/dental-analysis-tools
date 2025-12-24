import os
import sys
import requests


def main() -> int:
    base_url = os.getenv("PORTAL_SMOKE_BASE_URL", "http://localhost:8080")
    base_url = base_url.rstrip("/")

    health_url = f"{base_url}/api/upload-portal/health"
    uploads_url = f"{base_url}/uploads"
    verify_url = f"{base_url}/api/upload-portal/verify"

    failures = []

    try:
        resp = requests.get(health_url, timeout=10)
        if resp.status_code != 200:
            failures.append(f"health status {resp.status_code}")
        else:
            payload = resp.json()
            if not payload.get("ok"):
                failures.append("health response missing ok=true")
    except Exception as exc:
        failures.append(f"health error {exc}")

    try:
        resp = requests.post(verify_url, json={"token": "fake-token"}, timeout=10)
        if resp.status_code == 200:
            failures.append("verify expected failure but got 200")
    except Exception as exc:
        failures.append(f"verify error {exc}")

    try:
        resp = requests.get(uploads_url, timeout=10)
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            failures.append("uploads page not HTML")
    except Exception as exc:
        failures.append(f"uploads error {exc}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1

    print("OK: upload portal smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
