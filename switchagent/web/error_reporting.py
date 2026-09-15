"""W3-009: pure logic for the public error-reporting UX.

Builds the copyable issue-summary text and the pre-filled GitHub
"new issue" URL for the generic "Something went wrong" page (see
switchagent/web/app.py's global exception handler, which is the only
caller -- it gathers plain values from the diagnostics engine/DB/request
and hands them in here as an IssueSummaryInput; this module itself never
touches a database connection, WebContext, or the filesystem).

Never performs a network request of its own: build_report_issue_url()
only builds a URL string. The only thing that ever actually sends
anything is the user's own click on the resulting `target="_blank"` link,
followed by the user explicitly pressing Submit on GitHub's own page --
this module (and the template that renders its output) never auto-submits
anything, and never POSTs diagnostics/library/title-list/device-serial/
history/files anywhere.

Never includes a raw device_id -- only the safe fingerprint
(switchagent.mtp.windows.device_fingerprint()), the same convention every
other user-facing surface in this codebase already follows (see
diagnostics.py's own module docstring, and services.py's
recent_job_errors()/device_label() for the established
"fingerprint/friendly-name, never the raw string" rule this module
inherits unchanged).
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from urllib.parse import urlencode

# Never hardcode this into more than one place -- switchagent/web/app.py's
# exception handler and any test asserting the Report-issue link both
# import it from here.
GITHUB_NEW_ISSUE_URL = "https://github.com/Steklorez/Switch-Agent/issues/new"

_PLACEHOLDER = "—"  # em dash -- "unknown/not applicable", same convention diagnostics._format_size() uses


@dataclass(frozen=True)
class IssueSummaryInput:
    """Every field the W3-009 spec requires, already resolved to plain,
    already-safe strings by the caller. This module has no opinion on
    WHERE a value came from, only on how it's rendered -- and
    device_fingerprint must always be a fingerprint (see module docstring),
    never a raw device_id; that invariant is the caller's responsibility,
    exactly like every other diagnostics-adjacent call site in this
    codebase."""

    version: str = _PLACEHOLDER
    windows: str = _PLACEHOLDER
    runtime_mode: str = _PLACEHOLDER
    page_action: str = _PLACEHOLDER
    job_id: str = _PLACEHOLDER
    device_fingerprint: str = _PLACEHOLDER
    job_status: str = _PLACEHOLDER
    description: str = ""


def build_issue_summary_text(data: IssueSummaryInput) -> str:
    """Exactly the 8 fields the W3-009 spec calls for, in this order and
    with these exact labels -- a plain-text block meant to be pasted
    directly into a GitHub issue body (and to be pre-filled into one via
    build_report_issue_url() below)."""

    def _v(value: str) -> str:
        return value if value else _PLACEHOLDER

    lines = [
        f"SwitchAgent version: {_v(data.version)}",
        f"Windows: {_v(data.windows)}",
        f"Runtime mode: {_v(data.runtime_mode)}",
        f"Page/action: {_v(data.page_action)}",
        f"Job ID: {_v(data.job_id)}",
        f"Device fingerprint: {_v(data.device_fingerprint)}",
        f"Job status: {_v(data.job_status)}",
        f"Description: {data.description or ''}",
    ]
    return "\n".join(lines)


def build_report_issue_url(summary_text: str, *, base_url: str = GITHUB_NEW_ISSUE_URL) -> str:
    """Pre-fills GitHub's own "new issue" form via the `body` query
    parameter -- the user still reviews and explicitly submits on GitHub's
    own page. Never auto-submitted by this application; nothing here
    performs a network request itself (see module docstring)."""
    return f"{base_url}?{urlencode({'body': summary_text})}"


def windows_version_string() -> str:
    """Generic OS version info (e.g. "Windows-10-10.0.19045-SP0") -- not
    user- or device-identifying, safe to include verbatim in a public bug
    report. Additive: switchagent/diagnostics.py has no "Windows version"
    field at all, so this is a new, small, safe value -- never a second
    implementation of anything diagnostics.py already provides."""
    try:
        return platform.platform()
    except Exception:  # noqa: BLE001 -- must never be the reason the error page itself fails to render
        return _PLACEHOLDER
