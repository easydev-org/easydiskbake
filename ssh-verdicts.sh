#!/usr/bin/env bash
# Print the ssh stage's verdict for every -gui distribution, from its check run.
#
#     ./ssh-verdicts.sh            # the latest run of each workflow
#     ./ssh-verdicts.sh 42573025571 ...
#
# WHY A SCRIPT. The ssh stage is thirteen workflows whose names follow one pattern, and the
# question "which of them pass" is asked after every round of fixes. Reading it out of
# `gh run list` by hand means thirteen commands and thirteen chances to read the wrong run.
set -uo pipefail

WORKFLOWS="ubuntu-gui kubuntu-gui zorin-gui mint-gui mx-gui cachyos-gui manjaro-gui \
endeavour-gui fedora-gui opensuse-gui popos-gui nixos-gui ghostbsd-mate"

export https_proxy="${https_proxy:-http://mihomo.develop.svc.cluster.local:7890}"
export http_proxy="${http_proxy:-http://mihomo.develop.svc.cluster.local:7890}"

if [ "$#" -gt 0 ]; then
    RUNS="$*"
else
    RUNS=""
    for wf in $WORKFLOWS; do
        rid=$(gh run list --workflow="$wf.yml" --limit 1 --json databaseId -q '.[0].databaseId' 2>/dev/null)
        [ -n "$rid" ] && RUNS="$RUNS $rid"
    done
fi

printf '%-16s %-12s %-9s %s\n' WORKFLOW RUN CONCLUSION VERDICT
for rid in $RUNS; do
    wf=$(gh run view "$rid" --json workflowName -q .workflowName 2>/dev/null)
    state=$(gh run view "$rid" --json status,conclusion -q '"\(.status)/\(.conclusion // "-")"' 2>/dev/null)
    verdict=$(gh run view "$rid" --log 2>/dev/null \
        | grep -o 'VERDICT \(PASS\|FAIL\).*' | tail -1)
    [ -z "$verdict" ] && verdict="-"
    printf '%-16s %-12s %-9s %s\n' "$wf" "$rid" "$state" "$verdict"
done
