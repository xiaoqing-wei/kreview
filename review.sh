#!/usr/bin/env bash
# review.sh - DEPRECATED compatibility wrapper. Use ./kreview (see README.md).
#   ./review.sh <kernel_root> <file.c> [func_regex]     == kreview file --root <kernel_root> <file.c> --filter <regex>
#   DRY_RUN=1 ./review.sh ...                           == ... --dry-run
#   PATCH=my.patch ./review.sh <kernel_root>            == kreview patch --root <kernel_root> my.patch
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
KROOT=${1:?usage: review.sh <kernel_root> <file.c> [func_regex]}
extra=()
[[ "${DRY_RUN:-0}" == 1 ]] && extra+=(--dry-run)
[[ -n "${OUT_ROOT:-}" ]] && extra+=(--out "$OUT_ROOT")
[[ -n "${MAX_TOKENS:-}" ]] && extra+=(--max-bundle-tokens "$MAX_TOKENS")
[[ "${FORCE:-0}" == 1 ]] && extra+=(--no-cache)
if [[ -n "${PATCH:-}" ]]; then
  exec "$HERE/kreview" patch --root "$KROOT" "$PATCH" "${extra[@]}"
fi
FILE=${2:?usage: review.sh <kernel_root> <file.c> [func_regex]}
exec "$HERE/kreview" file --root "$KROOT" "$FILE" --filter "${3:-.}" "${extra[@]}"
