"""Standard-library HTTP client. Keys come from DEX_CLIENT_KEY, never CLI arguments."""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def call(url, key, body=None, headers=None):
    request = Request(url, data=body, headers={"Authorization": "Bearer " + key, **(headers or {})})
    for attempt in range(3):
        try:
            with build_opener(NoRedirect).open(request, timeout=75) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code not in {429, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"HTTP {exc.code}") from None
        except URLError, TimeoutError:
            if attempt == 2:
                raise RuntimeError(
                    "Connection failed; retry with the SAME idempotency key"
                ) from None
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError("Request failed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8001")
    parser.add_argument(
        "--task", choices=["invoice.extract", "delivery.extract"], default="invoice.extract"
    )
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--rotation", choices=["0", "90", "180", "270"], default="0")
    args = parser.parse_args()
    key = os.environ["DEX_CLIENT_KEY"]
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("task_id", args.task), ("rotation", args.rotation)):
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    parts.append(
        (
            f"--{boundary}\r\nContent-Disposition: form-data; "
            'name="file"; filename="document"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
    )
    parts.extend([args.file.read_bytes(), f"\r\n--{boundary}--\r\n".encode()])
    result = call(
        args.url.rstrip("/") + "/api/v1/documents/extract",
        key,
        b"".join(parts),
        {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Idempotency-Key": args.idempotency_key,
        },
    )
    print("job_id=" + result["job_id"], file=sys.stderr)
    deadline = time.monotonic() + 900
    while result["status"] in {"queued", "running"}:
        if time.monotonic() > deadline:
            raise RuntimeError(
                "Polling timeout. Resume GET using the recorded job_id; do not resubmit."
            )
        time.sleep(2)
        result = call(args.url.rstrip("/") + result["result_url"], key)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "succeeded" else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
