# Copilot instructions

<!-- CANONICAL — maintained in samoletovs/nauroLabs-github at config/copilot-pr-guard.md.
     Rolled out by scripts/install-pr-guard.ps1. Edit it there, not in the copy. -->

## Save and deliver each coding session

Use one task branch; parallel sessions editing the same repo need separate
worktrees. Fetch before starting, and never switch or update another session's
checkout. Commit only your own task changes and push meaningful checkpoints.
Before pausing, verify the remote contains your commit; an upstream alone is
not proof of publication.

For already approved work, finish testing and independent review, open a useful
PR and use the existing checked merge process without asking for the same
approval again. Do not bypass failed checks or privacy hooks.

If blocked, preserve the reviewed, non-sensitive changes on a pushed branch and
record the blocker on the issue. Do not open a PR until it is finished. Draft
status is not a hold: the janitor can un-draft Copilot PRs. An existing PR that
must remain held should be closed without deleting the saved branch.

Every coding handoff must state: commit, verified push, PR, tests, independent
review, merge, deployment and remaining work. "Implemented" is not "delivered".
Where the governance tools are available, run `scripts/session-finish.ps1`
against the exact checkout (`-RequireMerged` for delivery; `-Held` for a saved
unfinished branch). Otherwise verify those states with Git and GitHub directly.
Do not claim merged or deployed without evidence.

## Before you open a pull request

Measured across 131 merged PRs in this lab: **15% were self-declared `[WIP]` or
no-ops**. Each one still cost a full 10–30 minute agent run, and agent runs are
the single largest line in the lab's CI bill — around 63% of the monthly
allowance. A PR that says it isn't finished is the most expensive possible way to
report that you couldn't finish.

So: do not open a pull request unless all three of these are true.

**1. You changed behaviour.**
A change that only adds comments, reformats code, or restates the issue is not a
fix. If you discover the work is already done, **say so in a comment on the issue
and stop** — do not open a PR titled `No-op: already implemented`. The comment is
the useful artifact; the PR is noise that a human then has to close.

**2. You finished.**
Never open a PR titled `[WIP]`, `[Draft]`, or `Partial`. If something blocks you,
comment on the issue with: what you were trying to do, what you tried, the exact
error or ambiguity that stopped you, and what decision you need from a human.
That comment is worth more than a half-finished branch and costs a fraction as
much to act on.

**3. You verified it, and you say how.**
The PR description must state what you ran and what it printed. "Should work" and
"this should fix the issue" are not verification.

- If the repo has tests, add one that **fails without your change**. A test that
  passes either way certifies the implementation, not the requirement.
- If the change is not testable, say plainly what you checked by hand.
- If you could not verify it, say that too, in the description, rather than
  leaving it implied.

**Write the description properly.** It is the only part of your work that reaches
a human on a phone screen, and the merge gate refuses PRs whose body is empty or
boilerplate. Say what was broken, what you changed, and how you know it works.
