# Remote Sandbox Bridge

A small FastAPI service that dispatches isolated workloads to GitHub-hosted Ubuntu runners. It supports Python/Bash execution and Android Gradle APK builds, then exposes run status and GitHub artifact metadata to an Android client.

> **Security boundary:** this system intentionally runs arbitrary code. Use a dedicated private repository, a dedicated GitHub account/org, a narrowly-scoped PAT, a short workflow timeout, and a bridge API key. Do not place secrets in submitted scripts or build source. GitHub artifact URLs are authenticated GitHub API URLs, not anonymous public links.

## Repository layout

- `.github/workflows/runner.yml` — `workflow_dispatch` runner workflow.
- `app/main.py` — FastAPI bridge.
- `app/requirements.txt` — pinned runtime dependencies.
- `Dockerfile` — container deployment entrypoint.
- `android-example/RemoteSandboxApi.kt` — Retrofit/OkHttp client models and API.

## 1. GitHub repository setup

1. Create or choose a **private** GitHub repository and copy this repository's `.github/workflows/runner.yml` into it.
2. Ensure Actions are enabled and workflow dispatch is allowed.
3. Create a fine-grained GitHub PAT for the repository with the minimum required repository permission: **Actions: Read and write**. Contents: Read is needed by the checkout step. If your org requires it, approve the token for the org.
4. If the workflow is in the target repository, the workflow file must be present on the ref used below (`main` by default).
5. Confirm the runner can install Android dependencies and that the source project has `gradlew` executable or a system-compatible Gradle build.

The workflow accepts the required inputs `task_type`, `payload`, and `language`, plus a bridge correlation input `request_id`. The bridge generates the correlation ID and resolves GitHub's otherwise-empty dispatch response to the created run ID.

## 2. Configure the bridge

Create `.env` locally or configure the same values as secrets/environment variables on the hosting platform:

```dotenv
GITHUB_PAT=github_pat_xxxxxxxxxxxxxxxxxxxx
REPO_OWNER=your-github-user-or-org
REPO_NAME=your-private-runner-repo
GITHUB_REF=main
WORKFLOW_FILE=runner.yml
BRIDGE_API_KEY=replace-with-a-long-random-value
CORS_ORIGINS=https://your-app.example
MAX_PAYLOAD_CHARS=9000
DISPATCH_LOOKUP_TIMEOUT_SECONDS=15
```

Generate an API key with `openssl rand -hex 32`. Never commit `.env`, the PAT, or the bridge key. The workflow input limit is intentionally bounded; for larger scripts, put the source in a private repository and submit a controlled HTTPS source URL instead of expanding the dispatch payload.

## 3. Run locally

```bash
cd remote-sandbox-bridge
python3 -m venv .venv
. .venv/bin/activate
pip install -r app/requirements.txt
set -a; . ./.env; set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Health check:

```bash
curl http://localhost:8000/healthz
```

## 4. Deploy on Render, Koyeb, or Hugging Face Spaces

Use the repository as a Docker deployment. Set the service port to `8000` (or let the platform provide `PORT`) and add all `.env` values in the platform's secret/environment-variable settings. Do not bake secrets into the image.

- **Render:** New Web Service → repository → Docker → add environment variables → deploy.
- **Koyeb:** Create App → Dockerfile build from repository → expose port 8000 → add secret environment variables.
- **Hugging Face Spaces:** choose Docker, add the Dockerfile and environment secrets, and expose port 8000.

For production, put HTTPS in front of the service, restrict `CORS_ORIGINS` to exact origins, keep the bridge key private, and add platform rate limiting. The included CORS default allows no browser origins.

## 5. API examples

Start Python:

```bash
curl -X POST "$BRIDGE_URL/api/run-code" \
  -H "Content-Type: application/json" -H "X-API-Key: $BRIDGE_API_KEY" \
  -d '{"language":"python","script":"print(\"hello from GitHub\")"}'
```

Start an Android build from a public or appropriately accessible HTTPS Git/ZIP URL:

```bash
curl -X POST "$BRIDGE_URL/api/build-apk" \
  -H "Content-Type: application/json" -H "X-API-Key: $BRIDGE_API_KEY" \
  -d '{"source_url":"https://github.com/example/android-app.git"}'
```

Both return a response like:

```json
{"run_id":123456789,"request_id":"...","status":"queued"}
```

Poll until `status` is `completed`; for a successful run, `conclusion` is `success`. Then:

```bash
curl "$BRIDGE_URL/api/status/123456789" -H "X-API-Key: $BRIDGE_API_KEY"
curl "$BRIDGE_URL/api/artifacts/123456789" -H "X-API-Key: $BRIDGE_API_KEY"
```

The artifacts response contains `archive_download_url` values. Downloading them requires authentication to GitHub with a token that can read Actions artifacts. For a mobile app, do not put the GitHub PAT in the APK: add a backend download-proxy endpoint or have your app call the bridge with a short-lived app credential and let the bridge stream the artifact after authorizing the user.

## 6. Android client

Add Retrofit, OkHttp, Moshi, and coroutines to the Android app, then copy `android-example/RemoteSandboxApi.kt`. Use a base URL ending in `/`, e.g. `https://bridge.example.com/`. The example uses `X-API-Key`; for a multi-user app replace this shared-key design with user authentication and server-side authorization.

## 7. Verification checklist

1. `GET /healthz` returns `{"status":"ok"}`.
2. Submit a harmless script such as `print('ok')`; verify the response has a numeric `run_id`.
3. Poll `/api/status/{run_id}`; expected sequence is `queued` → `in_progress` → `completed` with `conclusion: success`.
4. Call `/api/artifacts/{run_id}`; verify `remote-sandbox-<request_id>` exists and includes `console.log`.
5. Submit a tiny Android sample with a Gradle wrapper; verify the artifact also includes an APK.
6. Test invalid API keys (401), HTTP build URLs (422), unsupported languages (422), and an invalid run ID (400).
7. Confirm the PAT is only present in host secrets, never in logs, client code, workflow inputs, or artifact files.

## Operational notes

- GitHub's free hosted-runner minutes and Actions artifact storage have account/repository limits; this design does not bypass GitHub quotas.
- Each job has a 30-minute timeout and artifacts expire after 7 days. Change both deliberately if your threat/cost model supports it.
- A GitHub Actions dispatch API call returns HTTP 204 without a run ID. The bridge uses a unique `request_id` in the workflow run name and polls briefly for the matching run. If GitHub is slow, a dispatch may be accepted while the bridge returns 504; inspect the Actions UI or retry using the request logs rather than blindly dispatching duplicates.
- GitHub workflow logs are not exposed by `/api/artifacts`; `console.log` is uploaded as an artifact. This avoids proxying large, token-protected log streams through the bridge.
