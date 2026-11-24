#!/usr/bin/env bash
# Reconcile the single repository-owned refresh branch with the resolved lock.
set -euo pipefail
body="${1:?Supply the release refresh report}"
security="${2:?Supply the security classification}"
branch=automation/refresh-build-inputs
repo="${GITHUB_REPOSITORY:?}"
git config user.name 'MertensiaOS dependency automation'
git config user.email 'mertensia-dependencies@users.noreply.github.com'
gh auth setup-git
git fetch origin main
if [[ "$(git rev-parse FETCH_HEAD)" != "$GITHUB_SHA" ]]; then
  echo 'main moved during upstream resolution. The next refresh uses its new revision.' >&2
  exit 1
fi
remote="$(git ls-remote --heads origin "refs/heads/$branch")"
read -r previous _ <<< "$remote"
if [[ -n "$previous" ]]; then
  git fetch origin "refs/heads/$branch"
fi
if [[ -z "$previous" || "$(git rev-parse "$previous^")" != "$GITHUB_SHA" ]] || ! git diff --quiet "$previous" -- build-inputs/lock.json build-inputs/config.json; then
  git add -- build-inputs/lock.json build-inputs/config.json
  git diff --cached --quiet && { echo 'Build inputs are unchanged'; exit 0; }
  git commit -m 'build(deps): refresh locked upstream inputs'
  git push --force-with-lease="refs/heads/$branch:$previous" origin "HEAD:refs/heads/$branch"
fi
head="$(git ls-remote --heads origin "refs/heads/$branch" | cut -f1)"
title='build(deps): refresh locked upstream inputs'
number="$(gh pr list --repo "$repo" --base main --head "$branch" --state open --json number --jq '.[0].number // empty')"
if [[ -z "$number" ]]; then
  gh pr create --repo "$repo" --base main --head "$branch" --title "$title" --body-file "$body"
  number="$(gh pr view --repo "$repo" "$branch" --json number --jq .number)"
else
  gh pr edit --repo "$repo" "$number" --title "$title" --body-file "$body"
fi
if [[ "$security" == true ]]; then
  label="$(gh label list --repo "$repo" --search security --json name --jq 'map(select(.name == "security")) | length')"
  if [[ "$label" == 0 ]]; then
    gh label create security --repo "$repo" --color B60205 --description 'Includes an upstream security fix'
  fi
  gh pr edit --repo "$repo" "$number" --add-label security
elif gh pr view --repo "$repo" "$number" --json labels --jq '.labels[].name' | grep -Fxq security; then
  gh pr edit --repo "$repo" "$number" --remove-label security
fi
printf 'head=%s\nnumber=%s\n' "$head" "$number" >> "${GITHUB_OUTPUT:?}"
