#!/usr/bin/env bash
# Regenerate the diagram SVGs from the Mermaid blocks embedded in the docs.
#
# The ```mermaid blocks inside the .md files are the single source of truth.
# This script extracts them and renders matching SVGs into docs/diagrams/ so the
# diagrams also display in plain markdown viewers (GitHub renders the Mermaid
# directly; everything else shows the committed SVG).
#
# Requires Node (npx); Mermaid CLI is fetched on demand. Run from anywhere:
#   bash docs/diagrams/regenerate.sh
set -euo pipefail
cd "$(dirname "$0")/../.."   # repo root

PUP="docs/diagrams/puppeteer.json"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Extract the Nth mermaid block (0-based) from a markdown file to a temp .mmd.
extract() {  # <md> <index> <outfile>
  python3 - "$1" "$2" "$3" <<'PY'
import re, sys
md, idx, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
blocks = re.findall(r"```mermaid\n(.*?)```", open(md).read(), re.S)
open(out, "w").write(blocks[idx])
PY
}

render() {  # <md> <name0> [name1 ...]  (one name per mermaid block, in order)
  local md="$1"; shift
  local i=0
  for name in "$@"; do
    extract "$md" "$i" "$TMP/$name.mmd"
    npx -y @mermaid-js/mermaid-cli -i "$TMP/$name.mmd" -o "docs/diagrams/$name.svg" -p "$PUP" -b white
    echo "rendered docs/diagrams/$name.svg"
    i=$((i+1))
  done
}

render docs/solution-architecture.md architecture-components
render docs/dataflow.md           dataflow-sequence dataflow-pipeline
echo "Done."
