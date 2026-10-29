import pytest
import os
import json
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI
from httpx import AsyncClient
from pathlib import Path

from med_api.router_factory import create_assessment_router

class DummySchema:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.mark.asyncio
async def test_load_json_data_success(tmp_path):
    service_mock = AsyncMock()
    router = create_assessment_router(
        prefix="/test",
        tags=["test"],
        schema=DummySchema,
        service=service_mock
    )
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(app=app, base_url="http://test") as client:
        data = {"field": 1}
        response = await client.post("/test/load-json-data/", json=data)

    service_mock.ingest.assert_awaited_once()
    assert response.status_code == 200
    assert "Data ingested successfully" in response.json()["message"]


@pytest.mark.asyncio
async def test_load_json_data_failure(tmp_path):
    service_mock = AsyncMock()
    service_mock.ingest.side_effect = Exception("fail")
    router = create_assessment_router(
        prefix="/test",
        tags=["test"],
        schema=DummySchema,
        service=service_mock
    )
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(app=app, base_url="http://test") as client:
        data = {"field": 1}
        response = await client.post("/test/load-json-data/", json=data)

    assert response.status_code == 500
    assert "fail" in response.json()["detail"]


@pytest.mark.asyncio
async def test_job_completed_creates_file(tmp_path):
    # Patch WATCHED_DIR to tmp_path
    with patch.dict(os.environ, {"WATCHED_DIR": str(tmp_path)}):
        router = create_assessment_router(
            prefix="/test",
            tags=["test"],
            schema=DummySchema,
            service=AsyncMock()
        )
        app = FastAPI()
        app.include_router(router)

        async with AsyncClient(app=app, base_url="http://test") as client:
            event = {"pipeline_id": "pipe123", "status": "success"}
            response = await client.post("/test/job-completed/", json=event)

        assert response.status_code == 200
        status_file = tmp_path / "pipe123.json"
        assert status_file.exists()
        with open(status_file) as f:
            data = json.load(f)
        assert data == {"pipeline_id": "pipe123", "status": "success"}


@pytest.mark.asyncio
async def test_job_completed_missing_fields(tmp_path):
    router = create_assessment_router(
        prefix="/test",
        tags=["test"],
        schema=DummySchema,
        service=AsyncMock()
    )
    app = FastAPI()
    app.include_router(router)

    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post("/test/job-completed/", json={})

    assert response.status_code == 400
    assert "pipeline_id and status required" in response.json()["detail"]
