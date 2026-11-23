#!/usr/bin/env bash
# Build the release zip: Kotodama/ at the top, ready to drop into
# Windower's addons folder.
#
#   scripts/build_release.sh [ref] [out_dir]
#
# ref      a tag (v1.2.3), branch or commit. Default: HEAD. For a tag, the tag
#          must match the version in Kotodama.lua and the sidecar.
# out_dir  where the zip and its .sha256 go. Default: dist/
#
# Only committed files go in (git archive), so local config, keys, caches and
# learned libraries never ship. Files marked export-ignore in .gitattributes
# (tests, CI, repo tooling) are left out too.
set -euo pipefail

ref="${1:-HEAD}"
out_dir="${2:-dist}"
cd "$(git rev-parse --show-toplevel)"

fail() { echo "build_release: $*" >&2; exit 1; }

git rev-parse --verify --quiet "$ref^{commit}" >/dev/null || fail "no such ref: $ref"

addon_version=$(git show "$ref:Kotodama.lua" | sed -n "s/^_addon\.version *= *'\([^']*\)'.*/\1/p")
sidecar_version=$(git show "$ref:server/kotodama/__init__.py" | sed -n 's/^__version__ *= *"\([^"]*\)".*/\1/p')
[ -n "$addon_version" ] || fail "no _addon.version in Kotodama.lua"
[ -n "$sidecar_version" ] || fail "no __version__ in server/kotodama/__init__.py"
[ "$addon_version" = "$sidecar_version" ] || \
    fail "Kotodama.lua says $addon_version but server/kotodama/__init__.py says $sidecar_version"

if git rev-parse --verify --quiet "refs/tags/$ref" >/dev/null; then
    # v1.2.0-rc1 may tag version 1.2.0 (or 1.2.0-rc1) as a prerelease.
    tag_version="${ref#v}"
    [ "$tag_version" = "$addon_version" ] || [ "${tag_version%%-*}" = "$addon_version" ] || \
        fail "tag $ref does not match version $addon_version"
    name="Kotodama-$ref"
else
    name="Kotodama-v$addon_version-$(git rev-parse --short "$ref")"
fi

mkdir -p "$out_dir"
zip="$out_dir/$name.zip"
rm -f "$zip" "$zip.sha256"
git archive --format=zip --prefix=Kotodama/ -o "$zip" "$ref"

# Check what went in. python3 is on every runner and needs no unzip.
python3 - "$zip" <<'EOF'
import fnmatch, sys, zipfile

names = zipfile.ZipFile(sys.argv[1]).namelist()
required = ["Kotodama/Kotodama.lua", "Kotodama/LICENSE", "Kotodama/server/kotodama_server.py",
            "Kotodama/library/npcs.default.json"]
forbidden = ["*/.git*", "*/.github/*", "*/.claude/*", "Kotodama/CLAUDE.md", "Kotodama/scripts/*",
             "Kotodama/tests/*", "Kotodama/server/tests/*", "*__pycache__*", "*.pyc",
             "Kotodama/server/config.json", "*/kotodama.env",
             # This install's own learned files: shipping them would overwrite
             # a user's on upgrade.
             "Kotodama/library/npcs.json", "Kotodama/library/cutscenes.json"]
problems = [f"missing {r}" for r in required if r not in names]
problems += [f"should not ship {n}" for n in names if any(fnmatch.fnmatch(n, p) for p in forbidden)]
problems += [f"outside Kotodama/: {n}" for n in names if not n.startswith("Kotodama/")]
if problems:
    sys.exit("build_release: " + "\n  ".join(["bad zip:"] + problems))
print(f"{len(names)} entries checked")
EOF

(cd "$out_dir" && sha256sum "$name.zip" > "$name.zip.sha256")
echo "$zip"
