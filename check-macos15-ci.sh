#!/bin/sh
# One command that answers "did all three YAML stages pass on CI?".
#
#     ./check-macos15-ci.sh 42559885009
#
# WHY THIS EXISTS
#
# The question this repository has been answering by hand for a dozen runs is not "is the run
# green" - it is "did the WALKTHROUGH do the work, and did each stage reach the end it is
# supposed to". A job can be green because a fallback path ran, and it can be red because a
# stage after it was skipped. So this reads, per job:
#
#     setup    17 step(s) answered   and  'the Setup Assistant is on screen'
#     desktop  all panes answered    and  'THE END: enter password'
#     ssh      'ssh answers'         and  every `ssh:` step exiting 0
#
# and prints the lines it found rather than a verdict nobody can check.
set -eu

RUN=${1:?usage: check-macos15-ci.sh <run-id>}
REPO=${GITHUB_REPOSITORY:-easydev-org/easydiskbake}

echo "=== $REPO run $RUN ==="
gh run view "$RUN" --repo "$REPO" --json status,conclusion,headSha \
  --jq '"run: \(.status) \(.conclusion // "") sha=\(.headSha[0:8])"'

echo
echo "=== per-job conclusions ==="
gh run view "$RUN" --repo "$REPO" --json jobs \
  --jq '.jobs[] | "  \(.name|.[0:28]|. + (" "*(28-length))) \(.status) \(.conclusion // "")"'

# THE CHECK RUNS ARE WHERE A RUNNING JOB'S LOG IS READABLE. The Actions log endpoint answers
# 404 until a job finishes, and these are kept current by log-stream while it runs.
SHA=$(gh run view "$RUN" --repo "$REPO" --json headSha --jq .headSha)
echo
echo "=== evidence, from the check runs ==="
gh api "repos/$REPO/commits/$SHA/check-runs" \
  --jq '.check_runs[] | "\(.name)\t\(.id)"' |
while IFS="$(printf '\t')" read -r name id; do
  case "$name" in
    macos15-setup|desktop-drive-*|ssh-enable|macos15-ssh) ;;
    *) continue ;;
  esac
  echo
  echo "--- $name ---"
  gh api "repos/$REPO/check-runs/$id" --jq '
    .output.text // ""
    | split("\n")
    | map(select(test("step\\(s\\) answered|THE END|ssh answers|ssh: .* -> [0-9]+|Setup Assistant is on screen|WALKTHROUGH|STUCK|FAILED|DID NOT")))
    | .[-12:]
    | .[]'
done
