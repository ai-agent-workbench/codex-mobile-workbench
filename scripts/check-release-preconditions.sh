#!/usr/bin/env bash
# Shared release gate: published code must come from an existing commit and a
# completely clean worktree.  APP_DIR is overridable only to make the guard
# independently testable in a temporary repository.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

if ! git -C "$APP_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "发布前置检查失败：$APP_DIR 不是 Git 工作区" >&2
    exit 1
fi

if ! git -C "$APP_DIR" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "发布前置检查失败：源码仓库还没有可发布的 commit" >&2
    exit 1
fi

dirty=$(git -C "$APP_DIR" status --porcelain --untracked-files=all)
if [[ -n "$dirty" ]]; then
    echo "发布前置检查失败：源码工作区不干净，请先提交或处理以下改动：" >&2
    printf '%s\n' "$dirty" >&2
    exit 1
fi

printf 'release_preconditions=passed head=%s\n' "$(git -C "$APP_DIR" rev-parse --short HEAD)"
