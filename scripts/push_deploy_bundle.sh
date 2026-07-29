#!/usr/bin/env bash
# Package deploy/ (compose + loki + grafana provisioning) and upload it to the
# telemetry bucket so EC2 user-data (infra/template.yaml) can bootstrap from it.
# Usage: scripts/push_deploy_bundle.sh <bucket-name>
set -euo pipefail

BUCKET="${1:?usage: push_deploy_bundle.sh <bucket-name>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

tar -czf "$tmp/deploy.tar.gz" -C "$ROOT" deploy
aws s3 cp "$tmp/deploy.tar.gz" "s3://$BUCKET/bootstrap/deploy.tar.gz"
echo "uploaded deploy bundle to s3://$BUCKET/bootstrap/deploy.tar.gz"
echo "on the instance: cd /opt/beacon/deploy && docker compose up -d"
