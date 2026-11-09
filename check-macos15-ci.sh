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

# --- THE STEP COUNTS HAVE TO MATCH THE FILES -----------------------------------------------
#
# '23 step(s) answered' is not evidence that the stage walked its walkthrough: a file whose
# steps were all skipped prints the same shape with a smaller number, and a stage that never
# ran its YAML at all prints nothing. Both have happened here - the ssh stage went green with
# 'the switch is on: did not appear' because a matcher read the wrong region, and the run
# before it was green with the walkthrough not reached at all.
#
# THE TWO SHAPES NEED DIFFERENT QUESTIONS, which is why this is not one comparison.
#
#   setup, ssh    SEQUENCES. Every named step runs or the walk stops, so the count must be
#                 the number of steps in the file. A smaller number is a walk that stopped
#                 early and still reported success.
#
#   desktop       A PANE SET. Panes are matched against the screen and the walk ends on an
#                 end condition, so the count is whatever the screen offered - 15 of the file's
#                 16 on the passing run, because macOS 15 never shows the keyboard-assistant
#                 pane when qemu supplies a USB keyboard. The question for this shape is not
#                 "did it answer all of them" but "did it get to the end at all", and that is
#                 read from `THE END` below.
echo
echo "=== did each stage run the whole file? ==="
FAILED=0
for pair in "macos15-setup:macos/15/steps/setup.yaml:sequence" \
            "desktop-drive-:macos/15/steps/sequoia.yaml:panes" \
            "ssh-enable:macos/15/steps/ssh.yaml:sequence"; do
  prefix=${pair%%:*}
  rest=${pair#*:}
  walk=${rest%%:*}
  kind=${rest##*:}

  if [ ! -f "$walk" ]; then
    echo "  FAIL  $walk is not in this checkout; cannot count its steps"
    FAILED=1
    continue
  fi

  want=$(grep -c '^  - name:' "$walk" || true)
  text=$(gh api "repos/$REPO/commits/$SHA/check-runs" \
         --jq ".check_runs[] | select(.name | startswith(\"$prefix\")) | .output.text // \"\"" \
         2>/dev/null)

  if [ "$kind" = "panes" ]; then
    if printf '%s' "$text" | grep -q "THE END:"; then
      got=$(printf '%s' "$text" | grep -oE '[0-9]+ step\(s\) answered' |
            grep -oE '[0-9]+' | tail -1)
      echo "  ok    $walk: reached its end condition, ${got:-?} of $want pane(s) matched"
      echo "        (a pane set answers what the screen offers; the end condition is the verdict)"
    else
      echo "  FAIL  $walk: no 'THE END' in this stage's log - the walk did not finish"
      FAILED=1
    fi
    # AND THE PANES THAT MUST HAVE RUN, SPECIFICALLY. Reaching the end is not enough: both CI
    # runs of the desktop stage before this change reached Finder while the ANALYTICS step had
    # never executed, because `data and privacy` matched that screen first and won on order.
    # The disk shipped with "Share Mac Analytics with Apple" ticked and every job was green.
    #
    # A pane that is always offered and answers something worth asserting belongs here. The
    # names are the `- name:` of the pane, which is what the walk logs as `=== <name>`.
    for needed in analytics "data and privacy"; do
      if printf '%s' "$text" | grep -q "=== $needed\$"; then
        echo "  ok    $walk: the '$needed' pane was answered"
      else
        echo "  FAIL  $walk: the '$needed' pane never ran - the stage may still be green"
        FAILED=1
      fi
    done
    continue
  fi

  got=$(printf '%s' "$text" | grep -oE '[0-9]+ step\(s\) answered' |
        grep -oE '[0-9]+' | tail -1)
  if [ -z "$got" ]; then
    echo "  FAIL  $walk wanted $want step(s); the stage never said how many it answered"
    FAILED=1
  elif [ "$got" -lt "$want" ]; then
    echo "  FAIL  $walk wanted $want step(s); the stage answered $got"
    FAILED=1
  else
    echo "  ok    $walk: $got step(s) answered (file has $want)"
  fi
done

echo
if [ "$FAILED" -ne 0 ]; then
  echo "=== A STAGE IS GREEN WITHOUT HAVING RUN ITS FILE - read the counts above ==="
  exit 1
fi
echo "=== every stage ran its file, or reached the end it declares ==="
