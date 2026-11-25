#!/usr/bin/env bash
# Reconcile the single repository-owned refresh branch with the resolved lock.
set -euo pipefail
body="${1:?Supply the release refresh report}"
security="${2:?Supply the security classification}"
branch=automation/refresh-build-inputs
repo="${GITHUB_REPOSITORY:?}"
api="repos/$repo"
title='build(deps): refresh locked upstream inputs'
inputs=(build-inputs/lock.json build-inputs/config.json)

gh auth setup-git
git fetch origin main
if [[ "$(git rev-parse FETCH_HEAD)" != "$GITHUB_SHA" ]]; then
  echo 'main moved during upstream resolution. The next refresh uses its new revision.' >&2
  exit 1
fi
git add -- "${inputs[@]}"
if git diff --cached --quiet "$GITHUB_SHA" -- "${inputs[@]}"; then
  echo 'Build inputs are unchanged'
  exit 0
fi

verified() {
  gh api "$api/commits/$1" --jq .commit.verification.verified
}

remote="$(git ls-remote --heads origin "refs/heads/$branch")"
read -r previous _ <<< "$remote"
head=
if [[ -n "$previous" ]]; then
  git fetch origin "refs/heads/$branch"
  if [[ "$(git rev-parse "$previous^")" == "$GITHUB_SHA" ]] &&
    git diff --cached --quiet "$previous"; then
    previous_verified=$(verified "$previous")
    if [[ "$previous_verified" == true ]]; then
      head="$previous"
    fi
  fi
fi
if [[ -z "$head" ]]; then
  # GitHub signs commits made through this mutation, which can only append to
  # a branch. Stage it on the exact main revision before updating the PR ref.
  staging="automation/refresh-signing/${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}"
  gh api --method POST "$api/git/refs" -f ref="refs/heads/$staging" -f sha="$GITHUB_SHA" >/dev/null
  trap 'gh api --method DELETE "$api/git/refs/heads/$staging" >/dev/null ||
    echo "Could not delete temporary signing branch $staging" >&2' EXIT
  head=$(
    python3 - "$repo" "$staging" "$GITHUB_SHA" "$title" "${inputs[@]}" <<'PY' |
import base64
import json
import subprocess
import sys

repo, branch, parent, title, *paths = sys.argv[1:]
changed = subprocess.check_output(
    ["git", "diff", "--cached", "--name-only", "-z", parent, "--", *paths]
).decode().strip("\0").split("\0")
additions = [
    {"path": path, "contents": base64.b64encode(
        subprocess.check_output(["git", "show", f":{path}"])
    ).decode("ascii")}
    for path in changed
]
json.dump({
    "query": """mutation($input: CreateCommitOnBranchInput!) {
        createCommitOnBranch(input: $input) { commit { oid } }
    }""",
    "variables": {"input": {
        "branch": {"repositoryNameWithOwner": repo, "branchName": branch},
        "expectedHeadOid": parent,
        "message": {"headline": title},
        "fileChanges": {"additions": additions},
    }},
}, sys.stdout)
PY
    gh api graphql --input - --jq .data.createCommitOnBranch.commit.oid
  )
  head_verified=$(verified "$head")
  if [[ "$head_verified" != true ]]; then
    echo 'GitHub did not verify the signature on the refresh commit.' >&2
    exit 1
  fi
  git fetch origin "refs/heads/$staging"
  if [[ "$(git rev-parse FETCH_HEAD)" != "$head" || "$(git rev-parse "$head^")" != "$GITHUB_SHA" ]] ||
    ! git diff --cached --quiet "$head"; then
    echo 'The signed refresh commit does not match the expected parent and contents.' >&2
    exit 1
  fi
  git push --force-with-lease="refs/heads/$branch:$previous" origin "$head:refs/heads/$branch"
fi
remote="$(git ls-remote --heads origin "refs/heads/$branch")"
if [[ "${remote%%$'\t'*}" != "$head" ]]; then
  echo 'The refresh branch moved before the PR could be updated.' >&2
  exit 1
fi
number=$(gh api --method GET "$api/pulls" -f base=main -f head="${repo%/*}:$branch" -f state=open \
  --jq '.[0].number // empty')
if [[ -z "$number" ]]; then
  number=$(gh api --method POST "$api/pulls" -f base=main -f head="$branch" \
    -f title="$title" -F body="@$body" --jq .number)
else
  gh api --method PATCH "$api/pulls/$number" -f title="$title" -F body="@$body" >/dev/null
fi
labels="$api/issues/$number/labels"
if [[ "$security" == true ]]; then
  label=$(gh api --paginate "$api/labels" --jq '.[] | select(.name == "security") | .name')
  if [[ -z "$label" ]]; then
    gh api --method POST "$api/labels" -f name=security -f color=B60205 -f description='Includes an upstream security fix' >/dev/null
  fi
  gh api --method POST "$labels" -F 'labels[]=security' >/dev/null
else
  label=$(gh api --paginate "$labels" --jq '.[] | select(.name == "security") | .name')
  if [[ -n "$label" ]]; then
    gh api --method DELETE "$labels/security" >/dev/null
  fi
fi
printf 'head=%s\nnumber=%s\n' "$head" "$number" >> "${GITHUB_OUTPUT:?}"
