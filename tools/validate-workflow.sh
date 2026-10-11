#!/usr/bin/env bash
set -euo pipefail

# The Actions validation job uses an amd64 Linux runner.
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'This validator requires Linux amd64; use actionlint directly on other platforms.' >&2
  exit 1
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
lint_tmp="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/hopegoo-actionlint.XXXXXX")"
trap 'rm -rf -- "$lint_tmp"' EXIT

# Pin the official release and its verified SHA-256; do not execute unchecked downloads.
archive=actionlint_1.7.12_linux_amd64.tar.gz
checksum=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8
curl --proto '=https' --tlsv1.2 --location --fail --silent --show-error \
  "https://github.com/rhysd/actionlint/releases/download/v1.7.12/$archive" \
  --output "$lint_tmp/$archive"
printf '%s  %s\n' "$checksum" "$lint_tmp/$archive" | sha256sum --check --status
tar --extract --gzip --file "$lint_tmp/$archive" --directory "$lint_tmp" actionlint
"$lint_tmp/actionlint" -shellcheck= -pyflakes= "$repo_root/.github/workflows/hopegoo-checkin.yml"
echo 'GitHub Actions configuration passed actionlint.'
