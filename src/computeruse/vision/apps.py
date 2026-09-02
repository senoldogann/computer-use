"""Pure, deterministic target-app inference from a goal string.

Autonomy (Law 5) does not require the user to name the app: the agent should
understand from the prompt which application the goal means. This module is
that understanding, as a pure function — no LLM call, no OS I/O, so it is
cheap, deterministic, and testable.

Two sources of intent, in priority order:

1. An explicit ``[App Name]`` prefix in the goal (the repo's established
   convention, e.g. ``[Google Chrome] Youtube'a git ...``). ``extract_goal_app``
   parses it and returns the cleaned goal for the provider.
2. Keyword matching of the goal against known application aliases
   (``infer_target_app``). Substring matching (not whole-word) is deliberate:
   goals are often agglutinative (Turkish ``Chrome'a git``, ``Excel'de aç``)
   and the app stem is what survives the suffix. Longer aliases win so
   ``"visual studio code"`` beats ``"code"``.

The inferred name is only ever a *candidate*: the driver's ``activate_app``
(``open -a``) validates it against LaunchServices, and a failure degrades to
the frontmost-app discovery with a warning — inference must never abort a run
the user could otherwise complete.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

# Canonical LaunchServices app name -> alias stems. Order is a tie-break only;
# specificity (alias length) ranks matches, so inserting a broad alias first
# never shadows a precise one later. Web services (``youtube``, ``gmail`` …)
# map to the first *running* browser via BROWSER_PREFERENCE, not to a single
# hardcoded browser.
APP_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "Google Chrome": ("google chrome", "chrome", "chromium"),
    "Safari": ("safari",),
    "Firefox": ("firefox", "mozilla firefox"),
    "Microsoft Edge": ("microsoft edge", "edge"),
    "Brave Browser": ("brave",),
    "Arc": ("arc browser",),
    "Finder": ("finder", "dosya yöneticisi", "file manager"),
    "Notes": ("notes",),
    "Mail": ("mail", "e-posta", "email"),
    "Calendar": ("calendar", "takvim"),
    "Photos": ("photos", "foto"),
    "Messages": ("messages", "imessage", "mesaj"),
    "Spotify": ("spotify",),
    "Music": ("apple music", "music app", "müzik"),
    "Slack": ("slack",),
    "Discord": ("discord",),
    "Telegram": ("telegram",),
    "WhatsApp": ("whatsapp", "wa"),
    "Zoom Workplace": ("zoom", "zoom.us"),
    "Microsoft Teams": ("microsoft teams", "teams"),
    "Notion": ("notion",),
    "Figma": ("figma",),
    "Visual Studio Code": ("visual studio code", "vs code", "vscode"),
    "Cursor": ("cursor",),
    "Xcode": ("xcode",),
    "IntelliJ IDEA": ("intellij",),
    "PyCharm": ("pycharm",),
    "Android Studio": ("android studio",),
    "Docker": ("docker",),
    "Postman": ("postman",),
    "iTerm2": ("iterm", "iterm2"),
    "Terminal": ("terminal", "komut satırı", "command line"),
    "Microsoft Excel": ("microsoft excel", "excel"),
    "Microsoft Word": ("microsoft word", "word"),
    "Microsoft PowerPoint": ("powerpoint",),
    "Numbers": ("numbers",),
    "Pages": ("pages",),
    "Keynote": ("keynote",),
    "Adobe Photoshop": ("photoshop",),
    "Adobe Premiere Pro": ("premiere pro", "premiere"),
    "Adobe Illustrator": ("illustrator",),
    "Final Cut Pro": ("final cut",),
    "Logic Pro": ("logic pro",),
    "OBS": ("obs",),
    "QuickTime Player": ("quicktime", "quick time"),
    "Preview": ("preview", "önizleme"),
    "TextEdit": ("textedit", "text edit"),
    "Calculator": ("calculator", "hesap makinesi"),
    "Maps": ("maps", "harita"),
    "System Settings": ("system settings", "ayarlar"),
    "Reminders": ("reminders", "hatırlatıcı"),
    "Podcasts": ("podcasts",),
    "TV": ("tv app", "apple tv"),
    "Books": ("apple books", "books"),
    "GarageBand": ("garageband",),
    "iMovie": ("imovie",),
    "Voice Memos": ("voice memos", "ses kaydı"),
}

# Web services the goal may name without mentioning a browser at all. The
# service is not an app, so the target becomes the first *running* browser in
# this preference order — the user's actual choice of browser wins, and only
# when no browser is running do we stay hands-off (None -> frontmost discovery).
BROWSER_SERVICES: Final[frozenset[str]] = frozenset(
    {
        "youtube",
        "gmail",
        "github",
        "gitlab",
        "netflix",
        "twitter",
        "x.com",
        "linkedin",
        "facebook",
        "instagram",
        "whatsapp web",
        "wikipedia",
        "reddit",
        "stackoverflow",
        "medium",
        "notion.so",
        "chatgpt",
        "claude",
        "google",
        "drive.google",
        "docs.google",
    }
)

# Preference order used only when a browser service is named and no explicit
# browser appeared in the goal. LaunchServices names, as `open -a` resolves.
BROWSER_PREFERENCE: Final[tuple[str, ...]] = (
    "Google Chrome",
    "Safari",
    "Firefox",
    "Microsoft Edge",
    "Brave Browser",
    "Arc",
)


def extract_goal_app(goal: str) -> tuple[str | None, str]:
    """Parse an explicit ``[App Name]`` prefix; return (app, cleaned goal).

    The repo convention puts the target app in square brackets at the start
    of the goal (``[Google Chrome] Youtube'a git ...``). When present, the
    prefix is authoritative and is stripped from the goal so the provider
    sees the task, not the bracket wrapper. Without a prefix, returns
    ``(None, goal)`` unchanged — the caller falls back to keyword inference.

    Pure: no I/O, no model call.
    """
    stripped = goal.strip()
    if not stripped.startswith("["):
        return None, goal
    close = stripped.find("]")
    if close == -1:
        return None, goal
    app = stripped[1:close].strip()
    if not app:
        return None, goal
    rest = stripped[close + 1 :].strip()
    return app, rest


def _matches_in(goal_lower: str) -> list[tuple[str, str]]:
    """Every (canonical app, alias) pair whose alias occurs in the goal.

    Substring match is intentional: agglutinative suffixes (``Chrome'a``,
    ``Excel'de``) break whole-word boundaries while the stem survives.
    Returns pairs only, sorted by alias length descending — the caller takes
    the first (most specific) match, so ``"visual studio code"`` beats
    ``"code"`` when both occur. Pure.
    """
    hits: list[tuple[str, str]] = []
    for canonical, aliases in APP_ALIASES.items():
        for alias in aliases:
            if alias in goal_lower:
                hits.append((canonical, alias))
    return sorted(hits, key=lambda pair: len(pair[1]), reverse=True)


def _browser_for_service(goal_lower: str, running_apps: Sequence[str]) -> str | None:
    """First running browser when the goal names a web service (pure)."""
    if not any(service in goal_lower for service in BROWSER_SERVICES):
        return None
    running = {name.lower() for name in running_apps}
    for candidate in BROWSER_PREFERENCE:
        if candidate.lower() in running:
            return candidate
    return None


def infer_target_app(goal: str, running_apps: Sequence[str]) -> str | None:
    """The most likely target application for a goal (pure, deterministic).

    Resolution order:

    1. Explicit ``[App]`` prefix (authoritative).
    2. Most specific alias match in the goal text (e.g. ``Excel'de aç`` ->
       ``Microsoft Excel``).
    3. A named web service (``youtube``, ``github`` …) with a running browser
       -> that browser; with no browser running, None (frontmost discovery).

    ``running_apps`` is the driver's live app list; it never *restricts* the
    result (``open -a`` can launch a not-running app), it only disambiguates
    service goals and is best-effort (empty on probe failure). ``None`` means
    \"the goal does not name an app — act on whatever is frontmost\".
    """
    explicit, _ = extract_goal_app(goal)
    if explicit is not None:
        return explicit
    goal_lower = goal.lower()
    matched = _matches_in(goal_lower)
    if matched:
        return matched[0][0]
    return _browser_for_service(goal_lower, running_apps)
