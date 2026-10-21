# Security Policy

## Reporting a vulnerability

Email **amanady125@gmail.com** with `[security]` in the subject — this is a
channel I actually monitor. You will get an acknowledgment within **72 hours**
and a fix-or-plan answer as soon as the diagnosis is done (target: one week
for a patch).

## The one rule that matters

**Never put tokens, cookies, API keys or account material in reports, issues,
patches or screenshots.** Describe the shape ("a 64-char hex string"), not the
value. Reports containing live credentials will be scrubbed and may be closed
as invalid — it protects you more than it protects the project.

## Scope

In scope: the bridge server itself (`app.py`, `functions.py`, `middleware.py`,
`plugin_helper.py`, `official_api.py`), the Docker image, the dashboard, and
the release workflow. Out of scope: DeepSeek's own services (report those to
DeepSeek), and anything requiring a public relay deployment — running one is
explicitly unsupported by this project.

## What a good report contains

Version/commit (the startup line), mode (bridge/official), a reproduction
starting from a clean clone or the prebuilt image, and the trace id from the
response or access log. No credentials.
