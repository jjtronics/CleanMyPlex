# CleanMyPlex Project Guidelines

## Architecture
- The application is a single Flask app centered in cleanmyplex.py.
- Keep changes minimal and consistent with the current monolith unless the task explicitly requests a refactor.
- Reuse existing connection helpers such as load_config(), refresh_connections(), connect_to_plex(), connect_to_account(), and ensure_required_connections().

## Conventions
- User-facing messages, labels, and flash messages should stay in French.
- Prefer app.logger over print for operational diagnostics.
- Keep configuration keys aligned with config.json.example and the settings form.
- Any change to a route that renders a template must keep route, template, form fields, and render_template context variables in sync.
- Any change to Plex authentication must support the current project rule: either PLEX_TOKEN, or PLEX_USERNAME with PLEX_PASSWORD.

## CSV And Tasks
- Reuse the existing CSV constants instead of hardcoding filenames in new code.
- Reuse the existing threaded task pattern with task_id, tasks, and tasks_lock for long operations.
- Avoid introducing new background execution models unless requested.

## Validation
- After editing Python or Jinja files, validate that the changed files have no syntax or template diagnostics.
- If you change settings, auth, CSV generation, or task status behavior, check both server logic and the related template.

## Safety
- Never log Plex tokens or passwords.
- Do not loosen route guards without checking ensure_required_connections() call sites.
- Do not commit assumptions about config.json being present; the app also falls back to config.json.example.