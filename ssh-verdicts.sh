#!/usr/bin/env bash
# Print the ssh stage's verdict for every -gui distribution, for ONE commit.
#
#     ./ssh-verdicts.sh 4776f66b      # the check runs for that commit
#     ./ssh-verdicts.sh               # the newest commit that has ssh check runs
#
# WHY BY COMMIT AND NOT BY WORKFLOW. Each workflow is dispatched several times while a fix is
# being tested, and `gh run list --workflow=X --limit 1` returns the NEWEST run - which is
# often a cancelled duplicate, so the table fills with `cancelled` and says nothing about the
# code. A commit's check runs are the set that was actually asked to run, one per job.
set -uo pipefail

export https_proxy="${https_proxy:-http://mihomo.develop.svc.cluster.local:7890}"
export http_proxy="${http_proxy:-http://mihomo.develop.svc.cluster.local:7890}"
REPO="${REPO:-easydev-org/easydiskbake}"

sha="${1:-}"
if [ -z "$sha" ]; then
    sha=$(gh api "repos/$REPO/commits?per_page=1" --jq '.[0].sha' | cut -c1-8)
fi

printf 'commit %s\n' "$sha"
printf '%-20s %-11s %-9s %s\n' JOB STATE RESULT VERDICT
gh api "repos/$REPO/commits/$sha/check-runs?per_page=100" \
    --jq '.check_runs[] | select(.name|endswith("-ssh")) | [.name, .status, (.conclusion // "-"), ((.output.text // "") | split("\n") | map(select(test("VERDICT|ssh: [0-9.]+:2222|PREROUTING jumps"))) | last // "-")] | @tsv' \
    | sort | while IFS=$'\t' read -r name state result verdict; do
        # The log line carries a stream prefix; keep only the interesting tail.
        verdict=$(printf '%s' "$verdict" | sed 's/.*\] *//')
        printf '%-20s %-11s %-9s %s\n' "$name" "$state" "$result" "$verdict"
    done
