"""Secure-ish, stateless bridge between a client and GitHub Actions.

The bridge never executes submitted code locally. GitHub-hosted runners execute it
in a fresh VM and the workflow applies a hard timeout and artifact retention policy.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, HttpUrl, field_validator

load_dotenv()

GITHUB_API = "https://api.github.com"
WORKFLOW_FILE = os.getenv("WORKFLOW_FILE", "runner.yml")
REPO_OWNER = os.getenv("REPO_OWNER", "")
REPO_NAME = os.getenv("REPO_NAME", "")
GITHUB_PAT = os.getenv("GITHUB_PAT", "")
BRIDGE_API_KEY = os.getenv("BRIDGE_API_KEY", "")
MAX_PAYLOAD_CHARS = int(os.getenv("MAX_PAYLOAD_CHARS", "9000"))
DISPATCH_LOOKUP_TIMEOUT_SECONDS = float(os.getenv("DISPATCH_LOOKUP_TIMEOUT_SECONDS", "15"))

app = FastAPI(title="Remote Sandbox Bridge", version="1.0.0")
allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or [],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


def require_config() -> None:
    if not REPO_OWNER or not REPO_NAME or not GITHUB_PAT:
        raise HTTPException(status_code=500, detail="Bridge is not configured")


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if BRIDGE_API_KEY and x_api_key != BRIDGE_API_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


async def github_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    require_config()
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {GITHUB_PAT}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url=GITHUB_API, timeout=20.0, follow_redirects=True) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
    if response.status_code >= 400:
        detail = response.text[:500]
        raise HTTPException(status_code=502, detail=f"GitHub API error {response.status_code}: {detail}")
    return response


class RunCodeRequest(BaseModel):
    language: Literal["python", "bash"]
    script: str = Field(min_length=1, max_length=MAX_PAYLOAD_CHARS)

    @field_validator("script")
    @classmethod
    def reject_nul(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("script contains NUL")
        return value


class BuildApkRequest(BaseModel):
    source_url: HttpUrl

    @field_validator("source_url")
    @classmethod
    def require_https(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("source_url must use HTTPS")
        return value


class DispatchResponse(BaseModel):
    run_id: int
    request_id: str
    status: str = "queued"


def workflow_path() -> str:
    return f"/repos/{REPO_OWNER}/{REPO_NAME}/actions/workflows/{WORKFLOW_FILE}/dispatches"


async def dispatch(payload: str, task_type: str, language: str) -> DispatchResponse:
    request_id = uuid.uuid4().hex
    ref = os.getenv("GITHUB_REF", "main")
    created_after = datetime.now(timezone.utc)
    response = await github_request(
        "POST",
        workflow_path(),
        json={
            "ref": ref,
            "inputs": {
                "task_type": task_type,
                "payload": payload,
                "language": language,
                "request_id": request_id,
            },
        },
    )
    if response.status_code != 204:
        raise HTTPException(status_code=502, detail="GitHub did not accept workflow dispatch")

    # workflow_dispatch returns 204 and no run ID. The unique run-name/request_id
    # is used to resolve the created run without exposing a PAT to the client.
    deadline = time.monotonic() + DISPATCH_LOOKUP_TIMEOUT_SECONDS
    query = f"?event=workflow_dispatch&per_page=20&created={created_after.date().isoformat()}"
    while time.monotonic() < deadline:
        runs = await github_request("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs{query}")
        for run in runs.json().get("workflow_runs", []):
            if request_id in (run.get("name") or ""):
                return DispatchResponse(run_id=run["id"], request_id=request_id)
        await asyncio.sleep(0.7)
    raise HTTPException(status_code=504, detail="Workflow accepted but run ID was not visible before timeout")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/run-code", response_model=DispatchResponse, dependencies=[Depends(require_api_key)])
async def run_code(request: RunCodeRequest) -> DispatchResponse:
    return await dispatch(request.script, "execute_script", request.language)


@app.post("/api/build-apk", response_model=DispatchResponse, dependencies=[Depends(require_api_key)])
async def build_apk(request: BuildApkRequest) -> DispatchResponse:
    return await dispatch(str(request.source_url), "build_apk", "python")


@app.get("/api/status/{run_id}", dependencies=[Depends(require_api_key)])
async def get_status(run_id: int) -> dict[str, Any]:
    if run_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid run ID")
    response = await github_request("GET", f"/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs/{run_id}")
    data = response.json()
    conclusion = data.get("conclusion")
    if data.get("status") != "completed":
        normalized = data.get("status")
    elif conclusion == "success":
        normalized = "completed"
    elif conclusion:
        normalized = "failed"
    else:
        normalized = "completed"
    return {
        "run_id": run_id,
        "status": normalized,
        "github_status": data.get("status"),
        "conclusion": conclusion,
        "html_url": data.get("html_url"),
        "run_name": data.get("name"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


@app.get("/api/artifacts/{run_id}", dependencies=[Depends(require_api_key)])
async def get_artifacts(
    run_id: int,
    include_expired: bool = Query(default=False),
) -> dict[str, Any]:
    if run_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid run ID")
    response = await github_request(
        "GET",
        f"/repos/{REPO_OWNER}/{REPO_NAME}/actions/runs/{run_id}/artifacts",
        params={"per_page": 100},
    )
    artifacts = []
    for item in response.json().get("artifacts", []):
        if item.get("expired") and not include_expired:
            continue
        artifacts.append(
            {
                "id": item["id"],
                "name": item["name"],
                "size_in_bytes": item.get("size_in_bytes"),
                "expired": item.get("expired", False),
                "created_at": item.get("created_at"),
                "expires_at": item.get("expires_at"),
                "download_url": item.get("archive_download_url"),
            }
        )
    return {"run_id": run_id, "artifacts": artifacts}
