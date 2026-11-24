# コーディングベンチ用標準サンドボックスイメージ。
# python:3.12-slim (debian bookworm) に curl / git / procps を足しただけの薄い層。
# node はランタイムでホストバイナリをファイル mount する (pi_runtime_mounts)。
# ビルド: ./scripts/coding-sandbox-build.sh
FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl git procps \
 && rm -rf /var/lib/apt/lists/*
