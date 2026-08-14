#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

git submodule sync -- vendor/link-pp
git submodule update --init --remote vendor/link-pp

if [[ "${1:-}" == "--rebuild" ]]; then
  docker compose -f deploy/link-pp.compose.yaml up -d --build
fi

git status --short -- .gitmodules vendor/link-pp
