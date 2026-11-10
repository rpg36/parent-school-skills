#!/usr/bin/env bash
# Run the tests, then build dist/<skill>.tar.gz for each skill, for copying to
# another machine.
#
#   ./package.sh                      # generic skills
#   ./package.sh --notes-dir DIR      # also bundle DIR/<skill>.md as references/local-notes.md
#
# Local notes are for site-specific guidance (your district's quirks) that
# shouldn't live in this repository.
set -euo pipefail
cd "$(dirname "$0")"

SKILLS=(canvas-grades parentvue-grades)
notes_dir=''
while [[ $# -gt 0 ]]; do
  case $1 in
    --notes-dir) notes_dir=${2:?--notes-dir needs a directory}; shift 2 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ -n ${notes_dir} && ! -d ${notes_dir} ]]; then
  echo "not a directory: ${notes_dir}" >&2; exit 2
fi

python3 -m unittest discover -s tests -q

mkdir -p dist
staging=$(mktemp -d)
trap 'rm -rf "${staging}"' EXIT

for skill in "${SKILLS[@]}"; do
  cp -R "skills/${skill}" "${staging}/"
  find "${staging}/${skill}" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -prune -exec rm -rf {} +
  if [[ -n ${notes_dir} && -f ${notes_dir}/${skill}.md ]]; then
    cp "${notes_dir}/${skill}.md" "${staging}/${skill}/references/local-notes.md"
    echo "Bundled local notes from ${notes_dir}/${skill}.md"
  fi
  COPYFILE_DISABLE=1 tar --no-xattrs -czf "dist/${skill}.tar.gz" -C "${staging}" "${skill}" 2>/dev/null \
    || COPYFILE_DISABLE=1 tar -czf "dist/${skill}.tar.gz" -C "${staging}" "${skill}"
  echo "Built $(pwd)/dist/${skill}.tar.gz"
  tar -tzf "dist/${skill}.tar.gz" | sed 's/^/  /'
done
