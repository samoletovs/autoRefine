"""Self-guided onboarding for autoRefine's browser interface."""

from __future__ import annotations

import html

_STEPS = (
    {
        "title": "Install autoRefine",
        "description": (
            "From the autoRefine checkout, install its Python dependencies. "
            "Python 3.11 or newer is required."
        ),
        "tooltip": (
            "Run this in the repository root. A virtual environment is recommended "
            "so the dependencies stay isolated from other projects."
        ),
        "code": "python -m pip install -r requirements.txt",
    },
    {
        "title": "Configure access",
        "description": (
            "Copy .env.example to .env, set FOUNDRY_PROJECT_ENDPOINT for planning, "
            "and sign in to Azure and GitHub so autoRefine can access its services."
        ),
        "tooltip": (
            "Use az login for local Azure credentials and gh auth login for repository "
            "cloning; GH_TOKEN/GITHUB_TOKEN can also provide GitHub access. Never commit "
            ".env or share its secret values."
        ),
        "code": ("cp .env.example .env\naz login\ngh auth login"),
    },
    {
        "title": "Describe your project",
        "description": (
            "Add a project.yaml to the root of the repository you want autoRefine "
            "to evaluate. Start with the project purpose, users, and goals."
        ),
        "tooltip": (
            "Quality checks are gated by the quality list. Declare the traits you "
            "want measured; an omitted trait is not treated as a passing check."
        ),
        "code": (
            "name: my-project\n"
            'purpose: "What the project does"\n'
            'users: "Who uses it"\n'
            "stage: active\n"
            "goals:\n"
            '  - "Your current goal"\n'
            "similar: []\n"
            "quality:\n"
            "  - tests\n"
            "  - ci-cd"
        ),
    },
    {
        "title": "Run your first evaluation",
        "description": (
            "Evaluate one repository and review the JSON report for findings, "
            "feature suggestions, and measurement coverage."
        ),
        "tooltip": (
            "Use the GitHub owner/repository slug. autoRefine needs access to clone "
            "the repository; configure GitHub credentials in your local environment."
        ),
        "code": "python -m agent.main --repo owner/repo --mode evaluate",
    },
    {
        "title": "Explore a plan safely",
        "description": (
            "Ask autoRefine to propose improvements. Planning uses Foundry and may "
            "incur model usage; dry-run previews idea filing without creating issues "
            "or sending notifications."
        ),
        "tooltip": (
            "Review suggestions before filing. A normal file-ideas run can create "
            "GitHub issues and send Telegram notifications."
        ),
        "code": (
            "python -m agent.main --repo owner/repo --mode plan\n"
            "python -m agent.main --repo owner/repo --mode file-ideas --dry-run"
        ),
    },
)


def render_onboarding_html() -> str:
    """Return a self-contained guided setup page with locally stored feedback."""
    cards = []
    for index, step in enumerate(_STEPS):
        cards.append(
            f'<article class="step" data-step="{index}"{" hidden" if index else ""}>'
            f"<h2>{html.escape(step['title'])} "
            f'<button class="tip-button" type="button" aria-label="More about this step" '
            f'aria-describedby="tip-{index}">?</button>'
            f'<span class="tooltip" id="tip-{index}" role="tooltip">'
            f"{html.escape(step['tooltip'])}</span></h2>"
            f"<p>{html.escape(step['description'])}</p>"
            f"<pre><code>{html.escape(step['code'])}</code></pre>"
            "</article>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Get started with autoRefine</title>
  <style>
    :root {{ color-scheme: light; font-family: system-ui, sans-serif; color: #1f2328; }}
    body {{ max-width: 760px; margin: 40px auto; padding: 0 20px; line-height: 1.55; }}
    .muted {{ color: #656d76; }}
    progress {{ width: 100%; height: 12px; accent-color: #0969da; }}
    article, section {{ margin: 20px 0; padding: 20px; border: 1px solid #d0d7de;
                        border-radius: 8px; background: #fff; }}
    h2 {{ margin-top: 0; }}
    pre {{ overflow-x: auto; padding: 14px; border-radius: 6px; background: #f6f8fa; }}
    .tip-button {{ border: 1px solid #8c959f; border-radius: 50%; width: 24px; height: 24px;
                   background: #fff; cursor: help; }}
    .tooltip {{ display: none; max-width: 32rem; margin: 8px 0; padding: 8px 12px;
                border-radius: 6px; background: #ddf4ff; font-size: .9rem; }}
    .tip-button:hover + .tooltip, .tip-button:focus + .tooltip,
    .tooltip:hover {{ display: block; }}
    nav, .feedback-actions {{ display: flex; gap: 10px; margin: 16px 0; }}
    button {{ padding: 8px 12px; border: 1px solid #d0d7de; border-radius: 6px;
              background: #f6f8fa; cursor: pointer; }}
    button:disabled {{ cursor: not-allowed; opacity: .55; }}
    fieldset {{ border: 0; padding: 0; }}
    label {{ margin-right: 14px; }}
    textarea {{ display: block; box-sizing: border-box; width: 100%; margin: 8px 0;
                padding: 8px; border: 1px solid #d0d7de; border-radius: 6px; }}
    [hidden] {{ display: none !important; }}
  </style>
</head>
<body>
  <h1>Welcome to autoRefine</h1>
  <p class="muted">Follow these steps to set up a project, evaluate it, and review a plan.</p>
  <label id="step-label" for="step-progress">Step 1 of {len(_STEPS)}</label>
  <progress id="step-progress" max="{len(_STEPS)}" value="1"></progress>
  <main id="steps">{"".join(cards)}</main>
  <nav aria-label="Onboarding steps">
    <button id="previous-step" type="button" disabled>Back</button>
    <button id="next-step" type="button">Next</button>
    <button id="finish-onboarding" type="button" hidden>Finish onboarding</button>
  </nav>
  <p id="completion-status" role="status" aria-live="polite"></p>
  <section aria-labelledby="feedback-title">
    <h2 id="feedback-title">Was this guide helpful?</h2>
    <p class="muted">Feedback and progress stay in this browser until you export them.</p>
    <fieldset>
      <legend>How clear was onboarding?</legend>
      <label><input type="radio" name="rating" value="clear"> Clear</label>
      <label><input type="radio" name="rating" value="somewhat"> Somewhat clear</label>
      <label><input type="radio" name="rating" value="unclear"> Unclear</label>
    </fieldset>
    <label for="feedback-comment">What should we improve? (optional)</label>
    <textarea id="feedback-comment" maxlength="500" rows="3"></textarea>
    <div class="feedback-actions">
      <button id="save-feedback" type="button">Save feedback</button>
      <button id="export-feedback" type="button">Download feedback JSON</button>
    </div>
    <p id="feedback-status" role="status" aria-live="polite"></p>
  </section>
  <script>
  (function() {{
    var key = 'autorefine.onboarding.v1';
    var count = {len(_STEPS)};
    var state = {{ currentStep: 0, completed: false, feedback: null }};
    try {{
      state = Object.assign(state, JSON.parse(localStorage.getItem(key) || '{{}}'));
    }} catch (_error) {{}}
    var current = Number.isInteger(state.currentStep)
      ? Math.min(Math.max(state.currentStep, 0), count - 1) : 0;
    var steps = Array.from(document.querySelectorAll('.step'));
    var progress = document.getElementById('step-progress');
    var label = document.getElementById('step-label');
    var previous = document.getElementById('previous-step');
    var next = document.getElementById('next-step');
    var finish = document.getElementById('finish-onboarding');
    var completion = document.getElementById('completion-status');
    function saveState() {{
      try {{
        localStorage.setItem(key, JSON.stringify(state));
        return true;
      }} catch (_error) {{
        return false;
      }}
    }}
    function showStep(index) {{
      current = Math.min(Math.max(index, 0), count - 1);
      steps.forEach(function(step, i) {{
        step.hidden = i !== current;
        step.setAttribute('aria-hidden', String(i !== current));
      }});
      state.currentStep = current;
      progress.value = current + 1;
      label.textContent = 'Step ' + (current + 1) + ' of ' + count;
      previous.disabled = current === 0;
      next.hidden = current === count - 1;
      finish.hidden = current !== count - 1;
      saveState();
    }}
    previous.addEventListener('click', function() {{ showStep(current - 1); }});
    next.addEventListener('click', function() {{ showStep(current + 1); }});
    finish.addEventListener('click', function() {{
      state.completed = true;
      state.completedAt = new Date().toISOString();
      completion.textContent = saveState()
        ? 'Onboarding complete — your progress is saved in this browser.'
        : 'Onboarding complete for this visit; browser storage is unavailable.';
    }});
    if (state.feedback) {{
      var savedRating = Array.from(document.querySelectorAll('input[name="rating"]'))
        .find(function(input) {{ return input.value === state.feedback.rating; }});
      if (savedRating) savedRating.checked = true;
      document.getElementById('feedback-comment').value = state.feedback.comment || '';
    }}
    document.getElementById('save-feedback').addEventListener('click', function() {{
      var rating = document.querySelector('input[name="rating"]:checked');
      var status = document.getElementById('feedback-status');
      if (!rating) {{
        status.textContent = 'Choose a clarity rating before saving.';
        return;
      }}
      state.feedback = {{
        rating: rating.value,
        comment: document.getElementById('feedback-comment').value.trim()
      }};
      state.feedbackAt = new Date().toISOString();
      status.textContent = saveState()
        ? 'Thank you — feedback is saved in this browser.'
        : 'Thank you — feedback is available to export for this visit.';
    }});
    document.getElementById('export-feedback').addEventListener('click', function() {{
      var status = document.getElementById('feedback-status');
      if (!state.feedback) {{
        status.textContent = 'Save a clarity rating before downloading feedback.';
        return;
      }}
      var payload = {{
        version: 1,
        completed: Boolean(state.completed),
        completedAt: state.completedAt || null,
        feedback: state.feedback,
        feedbackAt: state.feedbackAt || null
      }};
      var blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: 'application/json' }});
      var link = document.createElement('a');
      var downloadUrl = URL.createObjectURL(blob);
      link.href = downloadUrl;
      link.download = 'autorefine-onboarding-feedback.json';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function() {{ URL.revokeObjectURL(downloadUrl); }}, 1000);
      status.textContent = 'Feedback downloaded. Share the JSON file to help measure usability.';
    }});
    if (state.completed) {{
      completion.textContent = 'Onboarding complete — your progress is saved in this browser.';
    }}
    showStep(current);
  }})();
  </script>
</body>
</html>
"""
