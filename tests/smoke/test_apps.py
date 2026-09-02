"""Pure tests for autonomous target-app inference (no OS I/O — Law 6)."""

from __future__ import annotations

from computeruse.vision.apps import extract_goal_app, infer_target_app


def test_extract_goal_app_parses_bracket_prefix() -> None:
    app, goal = extract_goal_app("[Google Chrome] Youtube'a git ve 11. bölümü aç")
    assert app == "Google Chrome"
    assert goal == "Youtube'a git ve 11. bölümü aç"


def test_extract_goal_app_no_prefix_unchanged() -> None:
    assert extract_goal_app("Youtube'da ara") == (None, "Youtube'da ara")


def test_extract_goal_app_empty_or_unclosed_bracket_unchanged() -> None:
    assert extract_goal_app("[] boş") == (None, "[] boş")
    assert extract_goal_app("[açık olmayan") == (None, "[açık olmayan")


def test_infer_target_app_explicit_prefix_wins() -> None:
    goal = "[Notes] Alışveriş listesine yumurta ekle"
    assert infer_target_app(goal, running_apps=("Google Chrome",)) == "Notes"


def test_infer_target_app_matches_turkish_suffixes() -> None:
    # Agglutinative suffix after the app stem: whole-word matching would miss.
    assert infer_target_app("Chrome'a git ve yeni sekme aç", ()) == "Google Chrome"
    assert infer_target_app("Excel'de satır ekle", ()) == "Microsoft Excel"
    assert infer_target_app("Spotify'da çal", ()) == "Spotify"


def test_infer_target_app_most_specific_alias_wins() -> None:
    assert (
        infer_target_app("Visual Studio Code'da projeyi aç", ())
        == "Visual Studio Code"
    )


def test_infer_target_app_picks_running_browser_for_web_service() -> None:
    goal = "Youtube'da yeralti dizisini arat"
    # No browser running -> hands off (frontmost discovery takes over).
    assert infer_target_app(goal, running_apps=("Finder", "Notes")) is None
    # A running browser resolves the service goal.
    assert (
        infer_target_app(goal, running_apps=("Safari", "Finder")) == "Safari"
    )
    assert (
        infer_target_app(goal, running_apps=("Google Chrome", "Safari"))
        == "Google Chrome"
    )


def test_infer_target_app_none_when_no_signal() -> None:
    assert infer_target_app("yeni dosya oluştur", ("Finder",)) is None
    assert infer_target_app("", ()) is None
