#!/usr/bin/env bash
# コーディングベンチ用標準サンドボックスイメージをビルドする。
# 使い方: ./scripts/coding-sandbox-build.sh [タグ]
# 基底は python:3.12-slim (無ければ pull)。curl / git / procps 入りの
# llm-launcher/coding-sandbox:1 ができ、ベンチの既定イメージになる。
# 作らなくても python:3.12-slim があればフォールバックで動く (python3 は在る)。
set -euo pipefail
cd "$(dirname "$0")/.."
tag="${1:-llm-launcher/coding-sandbox:1}"
ctx="$(mktemp -d)"
trap 'rm -rf "$ctx"' EXIT
args=()
for v in HTTP_PROXY HTTPS_PROXY NO_PROXY http_proxy https_proxy no_proxy; do
  [ -n "${!v:-}" ] && args+=(--build-arg "$v=${!v}")
done
docker build "${args[@]}" -f scripts/coding-sandbox.Dockerfile -t "$tag" "$ctx"
echo "built: $tag"
