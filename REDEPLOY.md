# Redeploy & Env Update Guide (Cloud Run)

How to push changes to the live AgenticSearch service.

- **Service:** `agentic-search` · **Region:** `us-central1` · **Project:** `our-cursor-344414`
- **Live URL:** https://agentic-search-negglszkwa-uc.a.run.app
- Run all commands from the repo root (`Agentic Search/`).

## Mental model (read once)
There are **two copies** of your environment:
1. **Local `.env`** — the file you edit; used for local runs and read by the deploy script.
2. **The live service's env vars** — a snapshot taken at deploy time; this is what the running app actually uses.

Editing `.env` does **not** change the live service on its own. A **redeploy** (or an env update command) is what pushes values into the running service. Each change creates a new Cloud Run **revision** and traffic shifts automatically.

---

## Case 1 — I changed CODE (or want a full rebuild)
Rebuilds the image and ships a new revision. ~5–10 min.

```bash
cd "Agentic Search"
./deploy/cloudrun-deploy.sh
```

What it does: reads the API keys from `.env`, builds via Cloud Build, deploys. It prints the URL at the end.

> If `gcloud` ever isn't logged in: `gcloud auth login && gcloud config set project our-cursor-344414`.

---

## Case 2 — I only changed an ENV VAR or API KEY (no code change)
Fast path, **no rebuild** (~30s). Patches just the vars you name; leaves the rest.

Single var / key:
```bash
gcloud run services update agentic-search --region us-central1 \
  --update-env-vars OPENAI_API_KEY=the-new-value
```

Multiple at once (comma-separated, or repeat the flag):
```bash
gcloud run services update agentic-search --region us-central1 \
  --update-env-vars OPENAI_MODEL=gpt-4o-mini,MAX_RESULTS_PER_ANGLE=5
```

Remove a var:
```bash
gcloud run services update agentic-search --region us-central1 \
  --remove-env-vars SOME_VAR
```

> Keep `.env` in sync too (edit it to match), so the next full redeploy doesn't revert your change.
> Note: the deploy script in Case 1 only syncs the **keys** (`BRAVE_API_KEY`, `OPENAI_API_KEY`, optional `GROQ_API_KEY`) from `.env`; other settings are baked into the script. So for non-key vars, use the Case 2 command above (or edit the script).

---

## Handy commands
```bash
# Current live URL
gcloud run services describe agentic-search --region us-central1 --format='value(status.url)'

# See the env vars currently on the live service
gcloud run services describe agentic-search --region us-central1 \
  --format='value(spec.template.spec.containers[0].env)'

# Tail logs
gcloud run services logs read agentic-search --region us-central1 --limit 50

# Roll back to a previous revision
gcloud run revisions list --service agentic-search --region us-central1
gcloud run services update-traffic agentic-search --region us-central1 --to-revisions REVISION=100

# Tear down (stops all cost)
gcloud run services delete agentic-search --region us-central1
```

## Quick health check after any deploy
```bash
URL=$(gcloud run services describe agentic-search --region us-central1 --format='value(status.url)')
curl -s "$URL/api/health"   # -> {"status":"ok"}   (first hit after idle is slow: cold start)
```
