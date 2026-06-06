# Deploy AgenticSearch to Google Cloud Run

Runs the existing app unchanged on Cloud Run's free tier (scales to zero, so $0 when idle).
No pipeline changes, only the `Dockerfile` + deploy script.

## Prerequisites (one-time)
1. A Google account. Create a project at https://console.cloud.google.com (note the **Project ID**).
2. **Enable billing** on the project (free tier still requires a card; you stay within free limits).
3. Install the CLI: `brew install --cask google-cloud-sdk` (or use Cloud Shell in the browser, no install).
4. Log in:
   ```bash
   gcloud auth login
   ```

## Deploy
From the repo root:
```bash
export PROJECT_ID=your-project-id
export BRAVE_API_KEY=...      # or you'll be prompted
export OPENAI_API_KEY=...     # or you'll be prompted
# optional: export GROQ_API_KEY=...

chmod +x deploy/cloudrun-deploy.sh
./deploy/cloudrun-deploy.sh
```
First build takes ~5–10 min (torch + the cross-encoder are baked in). It prints your public URL when done.
On the first run, accept the prompt to create the `cloud-run-source-deploy` Artifact Registry repo.

## What the script sets
- Memory `2Gi`, CPU `1`, scales to zero (`--min-instances 0`), `--allow-unauthenticated`.
- Env vars mirroring the old DigitalOcean config: `APP_ENV`, `LOG_LEVEL`, `DB_PATH=/tmp/agentic_search.db`,
  `OPENAI_MODEL=gpt-4o-mini`, `PLANNER_PROVIDER`/`EXTRACTOR_PROVIDER=openai`, `JS_RENDERING_ENABLED=false`,
  plus the `BRAVE_API_KEY` / `OPENAI_API_KEY` secrets you provide.

## Notes
- **Cold start**: after idle, the first request is slow (~10–30s) while torch + the model load. That's the
  cost of staying at $0. To keep it always warm set `--min-instances 1` (this leaves the free tier and bills hourly).
- **Database** is ephemeral (`/tmp`), same behavior as before, fine for the demo (each cold start = fresh DB).
- **Custom domain**: `gcloud run domain-mappings create --service agentic-search --domain agentic.devaanand.com --region us-central1`.
- **Redeploy** after any change: just re-run `./deploy/cloudrun-deploy.sh`.
- **Tear down** (stop all cost): `gcloud run services delete agentic-search --region us-central1`.
- **Move keys to Secret Manager** later (optional, more secure than env vars): store each key as a secret and
  swap `--set-env-vars` for `--update-secrets BRAVE_API_KEY=brave-key:latest,OPENAI_API_KEY=openai-key:latest`.

## After it's live
Once you confirm it works, point your domain / links at the new URL and delete the DigitalOcean app to stop that bill.
