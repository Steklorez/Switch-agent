"""W3-007: onboarding logic -- pure functions, no I/O.

Given the application's already-known current state (library folder
configuration, library content count, known/connected devices), decides
which onboarding message (if any) applies. This module never touches the
filesystem, a database connection, or WebContext -- switchagent/web/app.py's
page routes are the only callers, and they are the ones that actually read
config.py/db.py/WebContext state and hand it in here as plain values (see
config.library_dir_info(), db.list_devices(), WebContext.get_known_devices()).

Onboarding is informational only (Web UI spec: "not a mandatory wizard" --
never blocks or gates access to Settings/History/Devices/Diagnostics).
Nothing in this module ever raises or signals "block this request" -- a
caller simply renders (or doesn't render) a banner alongside a page's
normal content; every page keeps working exactly the same regardless of
what (if anything) this module returns.

Every message this module can produce includes (in some form) the
required "SwitchAgent transfers content to DBI. DBI performs installation
on the console." semantics -- see INSTALL_SEMANTICS_LINE below -- so
onboarding copy never implies SwitchAgent itself performs or guarantees
the on-console install result. This mirrors the project's existing
DONE_UNVERIFIED transport-vs-install distinction (see
docs/ARCHITECTURE.md) without this module needing to touch that code at
all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

INSTALL_SEMANTICS_LINE = "SwitchAgent transfers content to DBI. DBI performs installation on the console."


@dataclass(frozen=True)
class OnboardingAction:
    label: str
    href: str


@dataclass(frozen=True)
class OnboardingMessage:
    id: str
    heading: str
    body: str
    steps: tuple[str, ...] = ()
    actions: tuple[OnboardingAction, ...] = ()


def compute_library_message(
    *,
    library_dir_configured: bool,
    library_dir_exists: bool,
    library_item_count: int,
) -> Optional[OnboardingMessage]:
    """Case 1 ("not configured or invalid") takes precedence over case 2
    ("configured but empty") -- an unset or broken folder is a more
    fundamental problem than "zero items found in an otherwise-fine
    folder", and case 2's own "no supported content found" wording would
    be actively misleading if the folder doesn't even exist yet.

    "Invalid" here means exactly what switchagent/config.py's
    library_dir_info() already means by `exists=False` -- a configured
    path that doesn't currently resolve to a real, existing directory
    (moved drive, deleted folder, etc.) -- not a new, separately-invented
    notion of validity."""
    if not library_dir_configured or not library_dir_exists:
        return OnboardingMessage(
            id="library_not_configured",
            heading="Choose Library Folder",
            body=(
                "SwitchAgent doesn't have a valid library folder yet. Choose the folder where your "
                "Switch content (NSP/NSZ/XCI/XCZ, Atmosphere mods) lives so SwitchAgent can find it. "
                + INSTALL_SEMANTICS_LINE
            ),
            actions=(OnboardingAction(label="Choose Library Folder", href="/settings#library-folder"),),
        )
    if library_item_count == 0:
        return OnboardingMessage(
            id="library_empty",
            heading="No supported content found",
            body=(
                "Your library folder is configured, but SwitchAgent hasn't found any supported content "
                "there yet. Add some NSP/NSZ/XCI/XCZ files or Atmosphere mods to it, then Rescan. "
                + INSTALL_SEMANTICS_LINE
            ),
            actions=(
                OnboardingAction(label="Rescan", href="#rescan"),
                OnboardingAction(label="Settings", href="/settings#library-folder"),
            ),
        )
    return None


def compute_device_message(
    *,
    known_device_count: int,
    any_device_connected: bool,
) -> Optional[OnboardingMessage]:
    """Case 3 ("no Switch ever seen") and case 4 ("known but currently
    disconnected") are deliberately different in tone (Web UI spec: don't
    scare a returning user with first-time-setup instructions just because
    they unplugged their console for a minute) -- case 4 never repeats the
    numbered first-time setup steps, and uses calmer, reconnect-only
    wording.

    A device that IS currently connected (known_device_count > 0 and
    any_device_connected is True) needs no onboarding message at all --
    that's the normal, working state -- so this returns None."""
    if known_device_count == 0:
        return OnboardingMessage(
            id="no_device_ever_seen",
            heading="Connect your Switch",
            body="SwitchAgent hasn't detected a Nintendo Switch yet. " + INSTALL_SEMANTICS_LINE,
            steps=(
                "Connect your Switch to this PC by USB.",
                "Open DBI on the Switch.",
                "Run MTP Responder from DBI.",
                "SwitchAgent will detect the console automatically.",
            ),
        )
    if not any_device_connected:
        return OnboardingMessage(
            id="device_known_but_disconnected",
            heading="Switch not connected",
            body=(
                "SwitchAgent has seen this Switch before, but it isn't connected right now. "
                "Reconnect it by USB and make sure DBI's MTP Responder is running when you're ready "
                "to transfer something again. " + INSTALL_SEMANTICS_LINE
            ),
        )
    return None
