#!/usr/bin/env bash
# Build the playground image with Cloud Build and deploy it to Cloud Run.
# Nothing project-specific lives in this file; it all comes from the environment:
#
#   PROJECT=<project id> REPO=<Artifact Registry docker repo> \
#   OWNER_PASS=<owner pass id> ./playground/deploy.sh
#
# Optional: REGION (us-central1), SERVICE (x3d-playground), BQ_TABLE
# (x3d_library.playground_runs), SECRET (playground-session-key), PHASE (2026-10),
# ADMIN_EMAILS (comma separated), RUNTIME_SA (default: the project's compute identity),
# OAUTH_CLIENT_ID (Google sign-in; can also be set later on the admin page).
#
# The service is reachable by anyone on the internet and admits no one: every
# page and API call needs a pass link or a Google sign-in on the access list.
# One instance (the journaled store keeps state in memory and BigQuery).
set -euo pipefail
cd "$(dirname "$0")/.."

: "${PROJECT:?set PROJECT}"
: "${REPO:?set REPO to an Artifact Registry docker repository}"
: "${OWNER_PASS:?set OWNER_PASS to the owner pass id}"
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-x3d-playground}
BQ_TABLE=${BQ_TABLE:-x3d_library.playground_runs}
SECRET=${SECRET:-playground-session-key}
PHASE=${PHASE:-2026-10}
COMMIT=$(git rev-parse --short HEAD)
IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/$SERVICE:$COMMIT"

gcloud builds submit --project "$PROJECT" --region "$REGION" \
  --config playground/cloudbuild.yaml --ignore-file playground/.gcloudignore \
  --substitutions "_IMAGE=$IMAGE,_COMMIT=$COMMIT" .

ENV_VARS="PLAYGROUND_STORE=journal|PLAYGROUND_BQ_TABLE=$BQ_TABLE|PLAYGROUND_PROJECT=$PROJECT"
ENV_VARS+="|PLAYGROUND_VERTEX_LOCATION=global|PLAYGROUND_PHASE=$PHASE|PLAYGROUND_BOOTSTRAP_PASS=$OWNER_PASS"
ENV_VARS+="|PLAYGROUND_VALIDATOR_COMMIT=$COMMIT"
[[ -n "${ADMIN_EMAILS:-}" ]] && ENV_VARS+="|PLAYGROUND_ADMIN_EMAILS=$ADMIN_EMAILS"
[[ -n "${OAUTH_CLIENT_ID:-}" ]] && ENV_VARS+="|PLAYGROUND_OAUTH_CLIENT_ID=$OAUTH_CLIENT_ID"

gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --allow-unauthenticated --min-instances 0 --max-instances 1 \
  --concurrency 40 --timeout 300 --cpu 1 --memory 1Gi --cpu-boost \
  ${RUNTIME_SA:+--service-account "$RUNTIME_SA"} \
  --labels "app=x3d-playground,validator=$COMMIT" \
  --set-env-vars "^|^$ENV_VARS" \
  --set-secrets "PLAYGROUND_SESSION_KEY=$SECRET:latest"
