#!/usr/bin/env bash
# Deploy AgenticSearch to Google Cloud Run (free tier, scales to zero).
# Pipeline code is unchanged; this only builds + deploys the container.
#
# Usage:
#   export PROJECT_ID=your-gcp-project
#   export BRAVE_API_KEY=...        # or you'll be prompted
#   export OPENAI_API_KEY=...       # or you'll be prompted
#   ./deploy/cloudrun-deploy.sh
#
# Run from the repo root.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-agentic-search}"

# Prompt for keys if not already exported (kept out of shell history).
if [[ -z "${BRAVE_API_KEY:-}" ]]; then read -rsp "BRAVE_API_KEY: " BRAVE_API_KEY; echo; fi
if [[ -z "${OPENAI_API_KEY:-}" ]]; then read -rsp "OPENAI_API_KEY: " OPENAI_API_KEY; echo; fi
GROQ_API_KEY="${GROQ_API_KEY:-}"

echo "→ Project: $PROJECT_ID  Region: $REGION  Service: $SERVICE"
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# Non-secret runtime config (matches the old DigitalOcean app.yaml).
ENVS="APP_ENV=production,LOG_LEVEL=INFO,DB_PATH=/tmp/agentic_search.db,OPENAI_MODEL=gpt-4o-mini,PLANNER_PROVIDER=openai,EXTRACTOR_PROVIDER=openai,JS_RENDERING_ENABLED=false,HF_HOME=/opt/hf,BRAVE_API_KEY=${BRAVE_API_KEY},OPENAI_API_KEY=${OPENAI_API_KEY}"
[[ -n "$GROQ_API_KEY" ]] && ENVS="${ENVS},GROQ_API_KEY=${GROQ_API_KEY},GROQ_MODEL=llama-3.3-70b-versatile"

# `^@^` changes the delimiter to @ so commas inside the list are unambiguous.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi \
  --cpu 1 \
  --timeout 600 \
  --min-instances 0 \
  --max-instances 3 \
  --concurrency 20 \
  --set-env-vars "^@^${ENVS//,/@}"

echo
echo "✓ Deployed. URL:"
gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)'
