import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import FakeProvider
from fastapi.testclient import TestClient
from filelock import Timeout

from document_extraction_api.app import create_app

HEADERS = {"Authorization": "Bearer " + "a" * 32, "Idempotency-Key": "test-1"}


def submit(client, data, key="test-1", task="invoice.extract", **kwargs):
    return client.post(
        "/api/v1/documents/extract",
        headers={**HEADERS, "Idempotency-Key": key},
        data={"task_id": task, **kwargs},
        files={"file": ("test", data)},
    )


def wait_result(client, response):
    assert response.status_code in {200, 202}, response.text
    url = response.json()["result_url"]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = client.get(url, headers=HEADERS)
        if result.json()["status"] not in {"queued", "running"}:
            return result.json()
        time.sleep(0.02)
    pytest.fail("worker failed to finish")


def test_bad_document_does_not_stop_next_job(settings, png):
    with TestClient(create_app(settings, FakeProvider)) as client:
        broken = submit(client, b"%PDF-corrupt", "broken")
        good = submit(client, png, "good")
        assert wait_result(client, broken)["error"]["code"] == "invalid_document"
        result = wait_result(client, good)
        assert result["status"] == "succeeded"
        assert result["data"]["total"] == "110"
        assert client.get("/health/ready").status_code == 200
        assert not (settings.data_dir / "files" / result["job_id"]).exists()


def test_auth_owner_and_task_permissions(settings, png):
    with TestClient(create_app(settings, FakeProvider)) as client:
        assert client.post("/api/v1/documents/extract").status_code == 401
        response = submit(client, png)
        url = response.json()["result_url"]
        other = {"Authorization": "Bearer " + "b" * 32, "Idempotency-Key": "other"}
        assert client.get(url, headers=other).status_code == 404
        assert (
            client.post(
                "/api/v1/documents/extract",
                headers=other,
                data={"task_id": "delivery.extract"},
                files={"file": ("x", png)},
            ).status_code
            == 403
        )


def test_concurrent_idempotency_and_conflict(settings, png):
    with TestClient(create_app(settings, FakeProvider)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: submit(client, png), range(2)))
        assert {r.status_code for r in responses} == {200, 202}
        assert len({r.json()["job_id"] for r in responses}) == 1
        assert submit(client, png + b"different").status_code == 409
        wait_result(client, responses[0])


def test_input_limits(settings, png):
    settings.max_file_bytes = 200
    with TestClient(create_app(settings, FakeProvider)) as client:
        assert submit(client, b"unknown").status_code == 415
        assert submit(client, b"x" * 201).status_code == 413
        assert submit(client, b"x" * 70000).status_code == 413
        assert submit(client, png, task="unknown").status_code == 422
        assert submit(client, png, rotation="45").status_code == 422
        assert submit(client, png, key="").status_code == 422


def test_single_process_lock(settings):
    with TestClient(create_app(settings, FakeProvider)):
        with pytest.raises(Timeout), TestClient(create_app(settings, FakeProvider)):
            pass


def test_expired_result_returns_gone(settings, png):
    app = create_app(settings, FakeProvider)
    with TestClient(app) as client:
        response = submit(client, png)
        result = wait_result(client, response)
        with app.state.store.db() as db:
            db.execute("UPDATE jobs SET updated=?", (time.time() - 8 * 86400,))
        app.state.store.cleanup(7, 90)
        r = client.get(response.json()["result_url"], headers=HEADERS)
        assert r.status_code == 410
        assert r.json()["data"] is None
        duplicate = submit(client, png)
        assert duplicate.json()["job_id"] == result["job_id"]
        assert duplicate.json()["result_expired"]


def test_delivery_and_openapi(settings, png):
    from conftest import invoice

    from document_extraction_api.tasks import Delivery

    class Provider(FakeProvider):
        async def extract(self, job, images):
            data = invoice().model_dump()
            del data["due_date"]
            data["delivery_date"] = "2026-10-27"
            data["total"] = None
            data["currency"] = None
            return Delivery.model_validate(data)

    with TestClient(create_app(settings, Provider)) as client:
        response = submit(client, png, task="delivery.extract")
        result = wait_result(client, response)
        assert result["status"] == "succeeded"
        assert result["data"]["delivery_date"] == "2026-10-27"
        schema = client.get("/openapi.json", headers=HEADERS).json()
        operation = schema["paths"]["/api/v1/documents/extract"]["post"]
        assert "multipart/form-data" in operation["requestBody"]["content"]
        assert operation["security"] == [{"ClientKey": []}]
        assert "JobResponse" in schema["components"]["schemas"]
