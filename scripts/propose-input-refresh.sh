#!/usr/bin/env bash
# Keep one dependency proposal for the main revision the lock was resolved on.
set -euo pipefail
body="${1:?Supply the release refresh report}"
security="${2:?Supply the security classification}"
base="${GITHUB_SHA:?}"
branch="automation/refresh-build-inputs-$base"
repo="${GITHUB_REPOSITORY:?}"
api="repos/$repo"
title='build(deps): refresh locked upstream inputs'
inputs=(build-inputs/lock.json build-inputs/config.json)

gh auth setup-git
check_main() {
  local remote
  remote=$(git ls-remote --exit-code origin refs/heads/main)
  if [[ "${remote%%$'\t'*}" != "$base" ]]; then
    echo 'main moved during upstream resolution. The next refresh uses its new revision.' >&2
    exit 1
  fi
}

open_proposals() {
  gh api --method GET --paginate "$api/pulls" -f base=main -f state=open \
    --jq '.[] | select(.head.repo.full_name == .base.repo.full_name)
          | select(.head.ref | test("^automation/refresh-build-inputs(-[0-9a-f]{40})?$"))
          | [.head.ref, .number] | @tsv'
}

close_superseded() {
  local keep="$1" proposals ref number
  proposals=$(open_proposals)
  while IFS=$'\t' read -r ref number; do
    [[ -n "$number" ]] || continue
    if [[ "$ref" == "$keep" ]]; then
      printf '%s\n' "$number"
    else
      gh api --method PATCH "$api/pulls/$number" -f state=closed >/dev/null
    fi
  done <<< "$proposals"
}

check_main
git add -- "${inputs[@]}"
if git diff --cached --quiet "$base" -- "${inputs[@]}"; then
  close_superseded ""
  echo 'Build inputs are unchanged'
  exit 0
fi

verified() {
  gh api "$api/commits/$1" --jq .commit.verification.verified
}

remote="$(git ls-remote --heads origin "refs/heads/$branch")"
previous="${remote%%$'\t'*}"
if [[ -z "$previous" ]]; then
  gh api --method POST "$api/git/refs" -f ref="refs/heads/$branch" -f sha="$base" >/dev/null
  previous="$base"
else
  git fetch origin "refs/heads/$branch"
  if [[ "$(git rev-parse FETCH_HEAD)" != "$previous" ]] ||
    ! git merge-base --is-ancestor "$base" "$previous" ||
    ! git diff --quiet "$base" "$previous" -- . ":(exclude)${inputs[0]}" ":(exclude)${inputs[1]}"; then
    echo 'The refresh branch changed or contains commits outside the resolved build inputs.' >&2
    exit 1
  fi
fi

head="$previous"
if ! git diff --cached --quiet "$previous"; then
  head=$(
    python3 - "$repo" "$branch" "$previous" "$title" "${inputs[@]}" <<'PY' |
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
  git fetch origin "refs/heads/$branch"
  if [[ "$(git rev-parse FETCH_HEAD)" != "$head" || "$(git rev-parse "$head^")" != "$previous" ]]; then
    echo 'The refresh branch moved before the signed commit could be checked.' >&2
    exit 1
  fi
fi
head_verified=$(verified "$head")
if [[ "$head_verified" != true ]] || ! git diff --cached --quiet "$head"; then
  echo 'The refresh commit is unverified or does not contain the resolved build inputs.' >&2
  exit 1
fi

remote=$(git ls-remote --exit-code origin "refs/heads/$branch")
if [[ "${remote%%$'\t'*}" != "$head" ]]; then
  echo 'The refresh branch moved before the PR could be updated.' >&2
  exit 1
fi
check_main
number=$(close_superseded "$branch")
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
printf 'head=%s\nbranch=%s\nnumber=%s\n' "$head" "$branch" "$number" >> "${GITHUB_OUTPUT:?}"
