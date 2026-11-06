#!/bin/sh
# Publish a release from this machine, taking everything from the GitLab release CI made for the tag:
#
#   ./publish.sh [--dry-run] TAG
#
# Downloads and verifies the wheels, uploads them to PyPI, then creates the GitHub release HACS reads, in
# that order so the version HACS offers can always be installed. --dry-run stops after the checks. A rerun
# skips what is already done.
#
# Needs curl, jq and uv. Credentials stay on this machine: twine reads the PyPI token (TWINE_PASSWORD or
# ~/.pypirc), and the GitHub token is GITHUB_TOKEN or else gh's.
set -eu
export LC_ALL=C

GITLAB_API=${GITLAB_API:-https://gitlab.com/api/v4/projects/jonathanduncan%2Fvtech_baby}
GITHUB_API=${GITHUB_API:-https://api.github.com/repos/Kasoo/vtech_baby}
GITHUB_GIT=${GITHUB_GIT:-https://github.com/Kasoo/vtech_baby.git}
PYPI_JSON=${PYPI_JSON:-https://pypi.org/pypi/vtech-link}
TWINE=${TWINE:-uvx twine@7.0.0}
MIRROR_WAIT=${MIRROR_WAIT:-600}   # seconds to wait for the tag to reach the GitHub mirror
MANIFEST=custom_components/vtech_baby/manifest.json

die() { echo "publish: $*" >&2; exit 1; }
confirm() {
  printf '%s [y/N] ' "$1"
  read -r answer < /dev/tty
  [ "$answer" = y ] || die "stopped"
}
github() { curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" -H "Accept: application/vnd.github+json" "$@"; }

dry_run=
if [ "${1:-}" = --dry-run ]; then dry_run=1; shift; fi
[ $# -eq 1 ] || die "usage: publish.sh [--dry-run] TAG"
tag=$1
cd "$(dirname "$0")"
work=.build/publish/$tag
rm -rf "$work"
mkdir -p "$work"

# The release and its assets.
status=$(curl -sS -o "$work/release.json" -w '%{http_code}' "$GITLAB_API/releases/$tag")
case $status in
  200) ;;
  404) die "no GitLab release for $tag; has the tag's pipeline finished?" ;;
  *) die "GitLab answered $status" ;;
esac
jq -r '.assets.links[] | [.name, (.direct_asset_url // .url)] | @tsv' "$work/release.json" > "$work/assets.tsv"
[ -s "$work/assets.tsv" ] || die "the GitLab release has no assets"
while IFS="$(printf '\t')" read -r name url; do
  curl -fsSL "$url" -o "$work/$name"
done < "$work/assets.tsv"

# The assets match their checksums and the tag's revision.
git fetch -q gitlab "+refs/tags/$tag:refs/tags/$tag"
rev=$(git rev-parse "$tag^{commit}")
[ "$(cat "$work/REVISION")" = "$rev" ] || die "the release was built from $(cat "$work/REVISION"), the tag is $rev"
(cd "$work" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch"

# One wheel per target, listed in SHA256SUMS, all one version.
[ "$(wc -l < "$work/SHA256SUMS")" -eq 4 ] || die "expected 4 wheels in SHA256SUMS"
for pattern in '*manylinux*_x86_64.whl' '*manylinux*_aarch64.whl' '*musllinux*_x86_64.whl' '*musllinux*_aarch64.whl'; do
  # shellcheck disable=SC2086
  [ "$(cd "$work" && ls $pattern 2>/dev/null | wc -l)" -eq 1 ] || die "expected one wheel matching $pattern"
done
version=$(cd "$work" && ls ./*.whl | cut -d- -f2 | sort -u)
[ "$version" = "$tag" ] || die "the wheels aren't all version $tag: $version"

# The tagged integration claims this version and pins these wheels.
git show "$rev:$MANIFEST" > "$work/manifest.json"
[ "$(jq -r .version "$work/manifest.json")" = "$tag" ] || die "manifest.json's version at $tag isn't $tag"
jq -e --arg pin "vtech-link==$version" '.requirements | index($pin)' "$work/manifest.json" > /dev/null \
  || die "manifest.json at $tag doesn't require vtech-link==$version"

$TWINE check --strict "$work"/*.whl

# PyPI files can't be replaced, so a wheel already there has to be this one.
status=$(curl -sS -o "$work/pypi.json" -w '%{http_code}' "$PYPI_JSON/$version/json")
case $status in
  200) ;;
  404) echo '{"urls":[]}' > "$work/pypi.json" ;;
  *) die "PyPI answered $status" ;;
esac
upload=
for wheel in "$work"/*.whl; do
  name=$(basename "$wheel")
  published=$(jq -r --arg name "$name" '.urls[] | select(.filename == $name) | .digests.sha256' "$work/pypi.json")
  ours=$(sha256sum "$wheel" | cut -d' ' -f1)
  if [ -z "$published" ]; then
    upload="$upload $wheel"
  elif [ "$published" = "$ours" ]; then
    echo "on PyPI already: $name"
  else
    die "PyPI has a different $name"
  fi
done

echo "$tag ($rev), vtech-link $version: verified"
if [ -n "$dry_run" ]; then
  echo "to upload to PyPI:${upload:- nothing}"
  exit 0
fi

if [ -n "$upload" ]; then
  confirm "Upload vtech-link $version to PyPI? This can't be undone."
  # shellcheck disable=SC2086
  TWINE_USERNAME=${TWINE_USERNAME:-__token__} $TWINE upload --non-interactive $upload
fi

# GitHub release: a draft until its assets are in, so HACS never sees it half made.
GITHUB_TOKEN=${GITHUB_TOKEN:-$(gh auth token)}
status=$(github -o /dev/null -w '%{http_code}' "$GITHUB_API/releases/tags/$tag")
if [ "$status" = 200 ]; then
  echo "GitHub release $tag exists already"
  exit 0
fi
[ "$status" = 404 ] || die "GitHub answered $status"

waited=0
until [ "$(git ls-remote "$GITHUB_GIT" "refs/tags/$tag" "refs/tags/$tag^{}" | tail -1 | cut -f1)" = "$rev" ]; do
  [ "$waited" -lt "$MIRROR_WAIT" ] || die "$tag didn't reach the GitHub mirror at $rev"
  sleep 15
  waited=$((waited + 15))
done

echo "On PyPI. Check it now: let a test Home Assistant install vtech-link==$version from PyPI."
confirm "Create GitHub release $tag? HACS offers it to users from then on."

# A draft left by a run that failed part-way.
github "$GITHUB_API/releases?per_page=100" \
  | jq -r --arg tag "$tag" '.[] | select(.draft and .tag_name == $tag) | .id' \
  | while read -r id; do github -f -X DELETE "$GITHUB_API/releases/$id"; done

jq --arg tag "$tag" '{tag_name: $tag, name: .name, body: .description, draft: true}' "$work/release.json" > "$work/github.json"
github -f -X POST "$GITHUB_API/releases" --data-binary "@$work/github.json" -o "$work/draft.json"
id=$(jq -r .id "$work/draft.json")
upload_url=$(jq -r '.upload_url | sub("\\{.*"; "")' "$work/draft.json")
for asset in "$work"/*.whl "$work/SHA256SUMS" "$work/REVISION"; do
  github -f -X POST -H "Content-Type: application/octet-stream" --data-binary "@$asset" \
    "$upload_url?name=$(basename "$asset")" -o /dev/null
done
github -f -X PATCH "$GITHUB_API/releases/$id" -d '{"draft": false}' -o "$work/published.json"
echo "published $(jq -r .html_url "$work/published.json")"
