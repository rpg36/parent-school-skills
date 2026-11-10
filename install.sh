#!/usr/bin/env bash
# Install the skills into one or more agents' skill folders.
#
#   ./install.sh --claude              # ~/.claude/skills        (Claude Code)
#   ./install.sh --codex               # ~/.agents/skills        (OpenAI Codex)
#   ./install.sh --dir PATH            # any other agent that reads SKILL.md folders
#   ./install.sh --claude --codex --skill canvas-grades
#
# Options:
#   --skill NAME       install only this skill (repeatable; default: all)
#   --notes-dir DIR    also copy DIR/<skill>.md to <skill>/references/local-notes.md
#   --link             symlink instead of copying (handy while developing)
#
# Copies replace any existing install of the same skill. Credentials are not
# touched; set them up afterwards with each script's `configure` command.
set -euo pipefail
cd "$(dirname "$0")"
src_root=$(pwd)/skills

ALL_SKILLS=(canvas-grades parentvue-grades)
targets=() skills=() notes_dir='' link=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --claude) targets+=("${HOME}/.claude/skills"); shift ;;
    --codex) targets+=("${HOME}/.agents/skills"); shift ;;
    --dir) targets+=("${2:?--dir needs a path}"); shift 2 ;;
    --skill) skills+=("${2:?--skill needs a name}"); shift 2 ;;
    --notes-dir) notes_dir=${2:?--notes-dir needs a directory}; shift 2 ;;
    --link) link=1; shift ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

[[ ${#targets[@]} -gt 0 ]] || { echo "choose at least one of --claude, --codex, --dir PATH (try --help)" >&2; exit 2; }
[[ ${#skills[@]} -gt 0 ]] || skills=("${ALL_SKILLS[@]}")
for s in "${skills[@]}"; do
  [[ -f ${src_root}/${s}/SKILL.md ]] || { echo "unknown skill: ${s}" >&2; exit 2; }
done
if [[ -n ${notes_dir} && ${link} -eq 1 ]]; then
  echo "--notes-dir can't be combined with --link (it would write into this checkout)" >&2; exit 2
fi

for target in "${targets[@]}"; do
  mkdir -p "${target}"
  for s in "${skills[@]}"; do
    dest="${target}/${s}"
    rm -rf "${dest}"
    if [[ ${link} -eq 1 ]]; then
      ln -s "${src_root}/${s}" "${dest}"
    else
      cp -R "${src_root}/${s}" "${dest}"
      find "${dest}" \( -name '__pycache__' -o -name '*.pyc' -o -name '.DS_Store' \) -prune -exec rm -rf {} +
      if [[ -n ${notes_dir} && -f ${notes_dir}/${s}.md ]]; then
        cp "${notes_dir}/${s}.md" "${dest}/references/local-notes.md"
      fi
      chmod 0755 "${dest}"/scripts/*.py
    fi
    echo "Installed ${dest}"
  done
done

cat <<EOF

Next, set up credentials yourself in a terminal (input is hidden; saved to
~/.config/school-skills/ with mode 0600):

EOF
for s in "${skills[@]}"; do
  script=$(ls "${src_root}/${s}/scripts/"*.py)
  echo "  python3 ${targets[0]}/${s}/scripts/$(basename "${script}") configure"
done
echo
echo "Then restart your agent (or start a new session) so it picks up the skills."
