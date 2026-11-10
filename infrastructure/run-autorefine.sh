#!/bin/sh
# Entry point for the autoRefine evaluation job.
#
# The *code* is cloned at start-up rather than baked into an image: that keeps the job on a
# public base image with no container registry to build, pay for, or keep in sync, and it
# means agent/ really is whatever is on master. Cost is ~30s of clone plus pip install.
#
# This file is the exception, and the exception is the whole trap. main.bicep inlines it
# with loadTextContent(), so production runs the copy captured by the last deployment:
# editing this file requires a redeploy to take effect. Merging is not enough, and nothing
# announces the gap, because from the outside nothing has gone wrong — the job simply keeps
# running the older script. The cost-telemetry block at the bottom shipped that way in #12
# and stayed inert, on a run that was otherwise executing new Python from master, and the
# only symptom was a file that never appeared. Change anything below and redeploy
# infrastructure/main.bicep.
#
# Every stage announces itself. Console-log ingestion is lossy, so these markers are what
# tell us how far the job actually got when something goes wrong.
set -eu

: "${GH_TOKEN:?GH_TOKEN not set}"

AUTH="https://x-access-token:${GH_TOKEN}@github.com"
WORK=/tmp/autorefine
# Pinned deliberately: an unpinned CLI is an unannounced dependency upgrade on every run.
GH_VERSION=2.63.2

# The agent shells out to `gh` for every repo operation (clone, issue, pr, label), and the
# python base image has git but not gh. Installed from the release tarball rather than apt
# so there is no third-party apt repo or signing key to manage. gh reads GH_TOKEN itself,
# so no `gh auth login` is needed.
echo "==> [1/6] installing GitHub CLI ${GH_VERSION}"
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" -o /tmp/gh.tgz
tar -xzf /tmp/gh.tgz -C /tmp
install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh
gh --version

echo "==> [2/6] cloning autoRefine + governance"
git clone --depth 1 "${AUTH}/samoletovs/autoRefine.git" /app
git clone --depth 1 "${AUTH}/samoletovs/nauroLabs-github.git" /gov

# Entrypoint drift. main.bicep inlines this file with loadTextContent(), so what is
# executing is the copy the last deploy captured, while /app was cloned from master a
# second ago. Both are on disk right now, which makes the one thing nobody could see --
# whether they still agree -- a comparison away. It runs here, before pip and the ~2h
# sweep, so a stale entrypoint is reported inside the first minute rather than after it.
#
# PID 1 is `/bin/sh -c <this script>`, because main.bicep supplies command and args
# separately, so /proc/1/cmdline holds the running copy NUL-separated:
#     /bin/sh \0 -c \0 <script> \0
# tr turns those separators into newlines. The script's own newlines are already
# newlines, so field 3 simply runs to the end of the output, and the only difference
# left between the two sides is trailing newlines -- one from the final NUL, one from
# the file's own last line. $(...) strips trailing newlines, from both sides, and that
# is the whole normalisation. Proven against a real NUL layout in
# tests/test_entrypoint_drift.py rather than reasoned about.
#
# Silent unless it can establish an answer. A wrong argv shape, an unreadable /proc, a
# missing clone, or a PID 1 that is not this script all say nothing at all, because a
# drift warning that fires when the check itself is broken is an alarm nobody reads.
# Absence of both lines below is what "could not check" looks like.
#
# The block is `|| true` because this diagnostic cannot always establish an answer; a
# missing or unreadable /proc entry is not itself a reason to stop the scheduled run.
# drift:begin -- the two paths below are substituted by the test
_drift_cmdline=/proc/1/cmdline
_drift_repo_copy=/app/infrastructure/run-autorefine.sh
_drift_shebang='#!/bin/sh'
# drift:config-end
{
  _drift_fields=$(tr '\000' '\n' < "${_drift_cmdline}" 2>/dev/null || true)
  _drift_running=$(printf '%s\n' "${_drift_fields}" | tail -n +3)

  # argv[1] must be -c or the layout is not the one this parses, and the running text
  # must open with our shebang or PID 1 is some other shell and not this script at all.
  if [ "$(printf '%s\n' "${_drift_fields}" | sed -n 2p)" = "-c" ] &&
     [ "$(printf '%s\n' "${_drift_running}" | sed -n 1p)" = "${_drift_shebang}" ] &&
     [ -r "${_drift_repo_copy}" ]
  then
    if [ "${_drift_running}" = "$(cat "${_drift_repo_copy}")" ]; then
      echo "==> entrypoint matches master"
    else
      echo "!!! ENTRYPOINT DRIFT: the running copy of run-autorefine.sh is not the one"
      echo "!!! on master. main.bicep bakes this file into the ARM template at deploy"
      echo "!!! time, so everything below is the version from the last deployment and"
      echo "!!! any change merged since is inert. Fix: redeploy infrastructure/main.bicep"
    fi
  fi
} || true
# drift:end

cd /app
echo "==> [3/6] validating the published budget policy"
if python -m agent.budget_policy --policy /gov/config/azure-budget.json; then
  echo "==> scheduled inference is permitted"
else
  policy_status=$?
  if [ "${policy_status}" -eq 3 ]; then
    echo "==> scheduled inference skipped by the budget policy"
    exit 0
  fi
  echo "!!! budget policy could not be validated; scheduled inference is disabled"
  exit "${policy_status}"
fi

echo "==> [4/6] installing dependencies"
# Not --quiet: if pip dies the last line before the silence is the only clue we get.
pip install --no-cache-dir --progress-bar off -r requirements.txt

mkdir -p "${WORK}"
echo "==> [5/6] evaluating all projects"
# The agent resolves scripts/file-idea.py and the wiki from the governance repo. Without
# this it silently files zero ideas, which is the entire point of file-ideas mode, and it
# re-clones governance once per project. The retired workflow used a .github-gov checkout;
# here it is already on disk.
export NAURO_GOVERNANCE_PATH=/gov
# One JSON row per Foundry run: mode, rounds, tokens, whether a cost guard fired. The
# run_cost log line carries the same numbers to stderr, but console ingestion drops lines
# (see above), so a distribution cannot be built from it. This file is committed once
# below, into the channel this script already treats as data.
COST_LOG=/tmp/autorefine-cost.jsonl
export AUTOREFINE_COST_LOG="${COST_LOG}"
# Mirrors the retired workflow: manifest-driven, file-ideas mode. The agent continues
# after per-project errors and writes a partial report. Keep its failure status while
# allowing report and cost persistence to finish; the next daily schedule is the retry,
# not a full container replay of completed inference.
REPORT_PATH=/tmp/autorefine-report.json
agent_status=0
# agent-run:begin
if python -m agent.main \
  --manifest /gov/config/workspace-manifest.json \
  --mode file-ideas \
  --workdir "${WORK}" \
  > "${REPORT_PATH}"; then
  echo "==> agent.main completed"
else
  agent_status=$?
  echo "!!! agent.main exited non-zero: ${agent_status}"
fi
# agent-run:end

echo "==> [6/6] report"
# Console-log ingestion drops lines, so counting the report objects is the only trustworthy
# measure of how many projects were actually processed.
echo "==> projects in report: $(grep -c '^  \"project\":' "${REPORT_PATH}" || echo 0)"
tail -c 4000 "${REPORT_PATH}" || true

# Persist the cost rows: one PUT, inside a job that has already run for ~2h, so this adds
# no scheduled run and no billed minute. A new file per sweep (same naming idea as the
# health report) means no read-modify-write and nothing to conflict on.
#
# Every step here is best-effort; publishing optional telemetry must not conceal the
# inference result.
if [ -s "${COST_LOG}" ]; then
  echo "==> committing $(wc -l < "${COST_LOG}") cost row(s)"
  {
    COST_FILE="reports/cost/run-$(date -u +%Y-%m-%d-%H%M).jsonl"
    gh api -X PUT "repos/samoletovs/nauroLabs-github/contents/${COST_FILE}" \
      -f message="chore(autorefine): run cost rows $(date -u +%Y-%m-%d)" \
      -f branch=master \
      -f content="$(base64 -w0 "${COST_LOG}")" \
      --silent && echo "==> cost rows committed: ${COST_FILE}"
  } || echo "!!! could not commit cost rows — continuing"
else
  echo "==> no cost rows to commit"
fi

# agent-status:begin
if [ "${agent_status}" -ne 0 ]; then
  echo "!!! autoRefine failed after report and cost persistence (exit ${agent_status})"
  exit "${agent_status}"
fi
# agent-status:end

echo "==> done"
