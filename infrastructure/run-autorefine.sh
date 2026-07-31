#!/bin/sh
# Entry point for the autoRefine evaluation job.
#
# The code is cloned at start-up rather than baked into an image: that keeps the job on a
# public base image with no container registry to build, pay for, or keep in sync, and it
# always runs whatever is on master. Cost is ~30s of clone plus pip install.
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
echo "==> [1/5] installing GitHub CLI ${GH_VERSION}"
curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" -o /tmp/gh.tgz
tar -xzf /tmp/gh.tgz -C /tmp
install -m 0755 "/tmp/gh_${GH_VERSION}_linux_amd64/bin/gh" /usr/local/bin/gh
gh --version

echo "==> [2/5] cloning autoRefine + governance"
git clone --depth 1 "${AUTH}/samoletovs/autoRefine.git" /app
git clone --depth 1 "${AUTH}/samoletovs/nauroLabs-github.git" /gov

cd /app
echo "==> [3/5] installing dependencies"
# Not --quiet: if pip dies the last line before the silence is the only clue we get.
pip install --no-cache-dir --progress-bar off -r requirements.txt

mkdir -p "${WORK}"
echo "==> [4/5] evaluating all projects"
# The agent resolves scripts/file-idea.py and the wiki from the governance repo. Without
# this it silently files zero ideas, which is the entire point of file-ideas mode, and it
# re-clones governance once per project. The retired workflow used a .github-gov checkout;
# here it is already on disk.
export NAURO_GOVERNANCE_PATH=/gov
# Mirrors the retired workflow: manifest-driven, file-ideas mode. Never fail the job on a
# single project's error — the agent already logs per-project failures and the report is
# written regardless. stderr stays on the console so progress is visible live; stdout is
# the report, so it is captured as data.
python -m agent.main \
  --manifest /gov/config/workspace-manifest.json \
  --mode file-ideas \
  --workdir "${WORK}" \
  > /tmp/autorefine-report.json || echo "!!! agent.main exited non-zero"

echo "==> [5/5] report"
# Console-log ingestion drops lines, so counting the report objects is the only trustworthy
# measure of how many projects were actually processed.
echo "==> projects in report: $(grep -c '^  \"project\":' /tmp/autorefine-report.json || echo 0)"
tail -c 4000 /tmp/autorefine-report.json || true
echo "==> done"
