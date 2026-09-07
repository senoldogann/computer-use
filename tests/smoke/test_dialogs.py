"""Popup / consent dialog detection tests.

A cookie wall, consent banner or sign-in sheet stops an autonomous run dead:
the model plans a click on page content the overlay covers, the click lands on
the dialog instead, and the recovery ladder burns the budget re-aiming at an
obstacle it was never told existed. These tests pin the pure detector — native
modal containers, keyword-gated web overlays across languages, and the
false-positive guard that keeps an ordinary page section from reading as a
modal — plus the prompt rendering that tells the agent to resolve overlays
FIRST.

Pure by design: fixtures are typed AXElement trees built in place, so no
driver, socket or host is involved.
"""

from __future__ import annotations

from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.prompts import ACTION_CONTRACT, state_context
from computeruse.vision import AXElement
from computeruse.vision.ax import (
    RecognizedLine,
    detect_dialogs,
    dialogue_element_summaries,
    dialogue_notes,
    ocr_dialog_notes,
    ocr_dialog_summaries,
    web_content_clipped_below,
)
from computeruse.vision.coordinates import Point, Rect, Size


def _button(
    title: str, x: float = 100, y: float = 100, *, width: float = 80, height: float = 24
) -> AXElement:
    """A clickable, titled button at a plausible position (fixture helper)."""
    return AXElement(
        role="Button",
        title=title,
        x=x,
        y=y,
        width=width,
        height=height,
    )


def _text(title: str) -> AXElement:
    """A static text node carrying dialog prose (fixture helper)."""
    return AXElement(role="StaticText", title=title, x=50, y=50, width=400, height=20)


def test_native_sheet_detected_with_controls() -> None:
    """A macOS Sheet is modal by construction: any button inside is reported."""
    sheet = AXElement(
        role="Sheet",
        title="Save changes?",
        x=300,
        y=200,
        width=400,
        height=200,
        children=(_text("You have unsaved changes."), _button("Save"), _button("Don't Save")),
    )
    window = AXElement(
        role="Window",
        title="Document",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(_button("Edit", x=50, y=30), sheet),
    )

    dialogs = detect_dialogs(window)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "Sheet"
    assert dialogs[0].label == "Save changes?"
    titles = [element.title for element in dialogs[0].elements]
    assert titles == ["Save", "Don't Save"]


def test_turkish_consent_overlay_detected() -> None:
    """webtekno's consent box — Turkish prose, decision buttons — is caught."""
    overlay = AXElement(
        role="Group",
        x=300,
        y=150,
        width=500,
        height=350,
        children=(
            _text("Webtekno olarak paylaştığınız kişisel verilerinizin güvenliğine önem veriyoruz"),
            _button("Ayarlar", x=340, y=420),
            _button("Reddet", x=430, y=420),
            _button("Kabul Et", x=540, y=420),
        ),
    )
    page = AXElement(
        role="WebArea",
        title="Yapay Zekâ Haberleri ve İçerikleri",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(
            _button("Popüler İçerikler", x=80, y=60),
            overlay,
            _button("Meta, Çalışanların Performansı", x=80, y=600),
        ),
    )

    dialogs = detect_dialogs(page)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "Overlay"
    assert [element.title for element in dialogs[0].elements] == [
        "Ayarlar",
        "Reddet",
        "Kabul Et",
    ]


def test_english_consent_overlay_detected() -> None:
    """The English cookie wall — 'Do not consent' / 'Consent' — is caught too."""
    overlay = AXElement(
        role="Group",
        x=250,
        y=120,
        width=520,
        height=400,
        children=(
            _text(
                "Webtekno.com asks for your consent to use your personal data: "
                "personalised advertising and content, audience research."
            ),
            _text("Your personal data will be processed and shared with 563 partners."),
            _button("Do not consent", x=280, y=460),
            _button("Consent", x=440, y=460),
        ),
    )
    page = AXElement(
        role="WebArea",
        title="webtekno.com/yapay-zeka",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(_button("Yapay Zekâ", x=60, y=40), overlay),
    )

    dialogs = detect_dialogs(page)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "Overlay"
    assert [element.title for element in dialogs[0].elements] == [
        "Do not consent",
        "Consent",
    ]


def test_sign_in_popover_detected() -> None:
    """The Google sign-in popover — native Popover with a 'continue' button."""
    popover = AXElement(
        role="Popover",
        title="webtekno.com oturumunu google.com ile açın",
        x=600,
        y=80,
        width=360,
        height=300,
        children=(
            _text("Senol Dogan — senoldogan0233@gmail.com"),
            _button("Şenol olarak devam et", x=640, y=330),
        ),
    )
    window = AXElement(
        role="Window",
        title="webtekno.com/yapay-zeka — Google Chrome",
        x=0,
        y=0,
        width=1200,
        height=800,
        children=(_button("Address bar", x=100, y=30), popover),
    )

    dialogs = detect_dialogs(window)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "Popover"
    assert dialogs[0].elements[0].title == "Şenol olarak devam et"


def test_plain_section_with_cookie_text_is_not_a_dialog() -> None:
    """Context alone (cookie prose, no decision control) must NOT flag a Group."""
    section = AXElement(
        role="Group",
        x=50,
        y=400,
        width=900,
        height=120,
        children=(_text("Bu sayfa çerezler kullanmaktadır."), _button("Detaylı bilgi", x=100, y=480)),
    )
    page = AXElement(
        role="WebArea",
        title="some-page",
        x=0,
        y=0,
        width=1000,
        height=800,
        children=(section,),
    )

    assert detect_dialogs(page) == ()


def test_plain_section_with_decision_button_is_not_a_dialog() -> None:
    """Decision alone (an 'Accept' button in ordinary content) must NOT flag it."""
    section = AXElement(
        role="Group",
        x=50,
        y=400,
        width=900,
        height=120,
        children=(_text("Sonbahar koleksiyonu geldi."), _button("Accept invitation", x=100, y=480)),
    )
    page = AXElement(
        role="WebArea",
        title="store",
        x=0,
        y=0,
        width=1000,
        height=800,
        children=(section,),
    )

    assert detect_dialogs(page) == ()


def test_sibling_dialogs_are_all_reported() -> None:
    """A sign-in popover AND a cookie wall on one page: both must surface.

    This is the exact multi-overlay case seen in the field — the run that
    prompted this detector had three overlays stacked on one screen.
    """
    popover = AXElement(
        role="Popover",
        title="oturumu google.com ile açın",
        x=600,
        y=80,
        width=360,
        height=300,
        children=(_button("Devam et", x=640, y=330),),
    )
    consent = AXElement(
        role="Group",
        x=250,
        y=120,
        width=520,
        height=400,
        children=(_text("Çerezler ve kişisel verileriniz"), _button("Kabul Et", x=440, y=460)),
    )
    page = AXElement(
        role="WebArea",
        title="page",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(consent, popover),
    )

    dialogs = detect_dialogs(page)

    assert len(dialogs) == 2
    assert {dialogue.kind for dialogue in dialogs} == {"Overlay", "Popover"}


def test_nested_container_is_not_double_reported() -> None:
    """A Group inside a reported Sheet must not produce a second dialogue."""
    inner = AXElement(
        role="Group",
        x=320,
        y=220,
        width=360,
        height=160,
        children=(_button("Cancel", x=340, y=340), _button("OK", x=440, y=340)),
    )
    sheet = AXElement(
        role="Sheet",
        title="Permission",
        x=300,
        y=200,
        width=400,
        height=200,
        children=(_text("Allow access?"), inner),
    )
    window = AXElement(
        role="Window",
        title="App",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(sheet,),
    )

    dialogs = detect_dialogs(window)

    assert len(dialogs) == 1
    assert [element.title for element in dialogs[0].elements] == ["Cancel", "OK"]


def test_controls_are_capped_per_dialogue() -> None:
    """A settings sheet with a dozen buttons reports only the first DIALOGUE_MAX_ELEMENTS."""
    buttons = tuple(_button(f"Option {index}", x=100, y=100 + index * 30) for index in range(12))
    sheet = AXElement(
        role="Sheet",
        title="Settings",
        x=300,
        y=200,
        width=400,
        height=500,
        children=buttons,
    )

    dialogs = detect_dialogs(sheet)

    assert len(dialogs[0].elements) == 8


def test_viewport_culls_dialog_controls() -> None:
    """Controls outside the observed display are not offered as click targets."""
    sheet = AXElement(
        role="Sheet",
        title="Confirm",
        x=300,
        y=200,
        width=400,
        height=200,
        children=(_button("Visible", x=340, y=240), _button("Off screen", x=340, y=5000)),
    )
    viewport = Rect(Point(0, 0), Size(1000, 800))

    dialogs = detect_dialogs(sheet, viewport=viewport)

    assert [element.title for element in dialogs[0].elements] == ["Visible"]


def test_dialogue_element_summaries_use_grounding_format() -> None:
    """Dialog controls render as ordinary AX summary lines with CENTRE points."""
    overlay = AXElement(
        role="Group",
        x=100,
        y=100,
        width=600,
        height=300,
        children=(
            _text("Kişisel verilerinizin korunması"),
            _button("Kabul Et", x=200, y=300, width=100, height=30),
        ),
    )

    lines = dialogue_element_summaries(detect_dialogs(overlay))

    assert len(lines) == 1
    # Centre of (200,300,100x30) is (250,315); the line must say so.
    assert lines[0] == 'Button "Kabul Et" at (250,315) 100x30'


def test_dialogue_notes_name_the_dialog_and_its_controls() -> None:
    overlay = AXElement(
        role="Group",
        x=100,
        y=100,
        width=600,
        height=300,
        children=(
            _text("Webtekno.com asks for your consent to use your personal data"),
            _button("Do not consent", x=200, y=300),
            _button("Consent", x=300, y=300),
            _button("Manage options", x=420, y=300),
            _button("List of partners", x=520, y=300),
        ),
    )

    notes = dialogue_notes(detect_dialogs(overlay))

    assert len(notes) == 1
    assert notes[0].startswith('Overlay "')
    assert "controls: Do not consent, Consent, Manage options, List of partners" in notes[0]


def test_dialogue_notes_cap_button_list() -> None:
    sheet = AXElement(
        role="Sheet",
        title="Many options",
        x=100,
        y=100,
        width=600,
        height=300,
        children=tuple(_button(f"B{index}", x=200, y=200 + index * 30) for index in range(7)),
    )

    notes = dialogue_notes(detect_dialogs(sheet))

    # max_buttons=4 default: exactly four labels, then the ellipsis marker.
    assert notes[0].endswith("B0, B1, B2, B3, …")
    assert "B4" not in notes[0]


def test_state_context_renders_dialog_section() -> None:
    """The prompt tells the agent overlays exist and to resolve them first."""
    text = state_context(
        WorkingState(
            goal="open the article",
            dialog_notes=('Overlay "Consent" — controls: Kabul Et, Reddet',),
        )
    )

    assert "POPUP DIALOGS DETECTED ON SCREEN" in text
    assert 'Overlay "Consent" — controls: Kabul Et, Reddet' in text
    assert "resolve these FIRST" in text


def test_cloudflare_challenge_overlay_detected() -> None:
    """A Cloudflare/CAPTCHA verification is a SecurityCheck, not a consent wall.

    The consent gate can never catch these — the page says "verify you are
    human", not "we use cookies" — which is exactly why the agent used to
    stare at the checkbox without clicking it.
    """
    overlay = AXElement(
        role="Group",
        x=300,
        y=150,
        width=500,
        height=350,
        children=(
            _text("Before you continue, check the box to verify you are human."),
            AXElement(
                role="CheckBox",
                title="Verify you are human",
                x=340,
                y=420,
                width=200,
                height=24,
            ),
        ),
    )
    page = AXElement(
        role="WebArea",
        title="webtekno.com",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(overlay,),
    )

    dialogs = detect_dialogs(page)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "SecurityCheck"
    assert dialogs[0].elements[0].title == "Verify you are human"


def test_consent_overlay_keeps_overlay_kind() -> None:
    """A consent wall stays an Overlay; only verification challenges become SecurityCheck."""
    overlay = AXElement(
        role="Group",
        x=300,
        y=150,
        width=500,
        height=350,
        children=(_text("Çerezler ve kişisel verileriniz"), _button("Kabul Et", x=440, y=460)),
    )
    page = AXElement(
        role="WebArea",
        title="page",
        x=0,
        y=0,
        width=1000,
        height=700,
        children=(overlay,),
    )

    dialogs = detect_dialogs(page)

    assert len(dialogs) == 1
    assert dialogs[0].kind == "Overlay"


def _ocr_line(text: str, y: float) -> RecognizedLine:
    """An OCR line at a given vertical position (fixture helper)."""
    return RecognizedLine(
        text=text,
        confidence=0.9,
        x=100.0,
        y=y,
        width=400.0,
        height=24.0,
    )


def test_ocr_detects_consent_dialog_when_ax_is_blind() -> None:
    """The blind-app path: OCR text alone must surface the cookie wall."""
    lines = (
        _ocr_line("Webtekno olarak kişisel verilerinizin güvenliğine önem veriyoruz", 300),
        _ocr_line("Kabul Et", 340),
        _ocr_line("Reddet", 340),
        _ocr_line("Ana sayfaya dön", 500),
    )

    notes = ocr_dialog_notes(lines)
    summaries = ocr_dialog_summaries(lines)

    assert len(notes) == 1
    assert notes[0].startswith('Overlay "')
    assert "Kabul Et" in notes[0] and "Reddet" in notes[0]
    # Decision lines become clickable marks; unrelated lines stay out.
    assert len(summaries) == 2
    assert "Kabul Et" in summaries[0]
    assert "Ana sayfaya dön" not in summaries[0]


def test_ocr_detects_security_check() -> None:
    """OCR must also catch Cloudflare/CAPTCHA challenges."""
    lines = (
        _ocr_line("Checking your browser before accessing webtekno.com", 200),
        _ocr_line("Verify you are human", 240),
    )

    notes = ocr_dialog_notes(lines)

    assert len(notes) == 1
    assert notes[0].startswith('SecurityCheck "')
    assert "Verify you are human" in notes[0]


def test_ocr_ignores_mentions_without_nearby_decisions() -> None:
    """A page that merely mentions cookies next to an unrelated button is not flagged."""
    lines = (
        _ocr_line("Gizlilik Politikası", 100),
        _ocr_line("İletişim", 800),  # far below the context line
    )

    assert ocr_dialog_notes(lines) == ()


def test_web_content_clipped_below_detects_scrollable_pages() -> None:
    """A page whose document extends past the viewport bottom must hint scrolling."""
    viewport = Rect(Point(0, 0), Size(1200, 800))
    long_page = AXElement(
        role="WebArea",
        title="long",
        x=0,
        y=0,
        width=1200,
        height=2400,
    )
    assert web_content_clipped_below(long_page, viewport) is True

    fitting_page = AXElement(
        role="WebArea",
        title="short",
        x=0,
        y=0,
        width=1200,
        height=700,
    )
    assert web_content_clipped_below(fitting_page, viewport) is False
    # No viewport observed: no hint, no false positive.
    assert web_content_clipped_below(long_page, None) is False


def test_action_contract_guides_dialog_handling() -> None:
    """The system contract instructs resolving dialogs before task actions."""
    assert "When POPUP DIALOGS are listed above, resolve them BEFORE any task action" in ACTION_CONTRACT
    assert "never try to reach content a dialog covers" in ACTION_CONTRACT
    assert "closed or dismissed, not filled in" in ACTION_CONTRACT
    assert "SecurityCheck is a Cloudflare/CAPTCHA verification" in ACTION_CONTRACT
    assert "the page extends below the visible area" in ACTION_CONTRACT


def test_state_context_without_dialogs_mentions_none() -> None:
    text = state_context(WorkingState(goal="x", ui_elements=('Button "Hi" at (1,2) 3x4',)))
    assert "POPUP DIALOGS" not in text

def test_manage_consent_launcher_is_not_a_blocking_overlay() -> None:
    launcher = AXElement(
        role="Group", x=1400, y=950, width=200, height=50,
        children=(_button("Manage consent", x=1450, y=960),),
    )
    assert detect_dialogs(launcher) == ()
    opened = AXElement(
        role="Group", x=1000, y=700, width=600, height=300,
        children=(_text("We use cookies"), _button("Accept"), _button("Manage consent")),
    )
    assert len(detect_dialogs(opened)) == 1
