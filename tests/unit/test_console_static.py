"""Static pins on the ops console's embedded JS (re-audits `15d875d` F6, `cbb783b` F6).

The console is a single static file with no JS test harness, so the DOM's authority expressions
are pinned at the source level. Whole-file substring presence is NOT enough — it passes when the
right token exists anywhere and the wrong one exists in the function that matters — so every
assertion here is scoped to the body of the specific renderer under test.
"""

import re
from pathlib import Path

CONSOLE = (
    Path(__file__).resolve().parents[2] / "src" / "kyc_tool" / "ui" / "console.html"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    """The source of one `async function <name>(...)`, from its header to the next top-level
    `async function` (the file's renderers are all declared at column 0)."""
    start = re.search(rf"^async function {name}\(", CONSOLE, re.MULTILINE)
    assert start, f"renderer {name}() not found — the pin is scoped to a function that moved"
    rest = CONSOLE[start.end():]
    end = re.search(r"^async function ", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    # `//` comments are stripped: a line RECORDING a correction ("… c.latest_decision goes stale
    # …") is the fix, not a reassertion of it, and must not trip a ban on the expression itself.
    return "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())


VIEW_CASE = _function_body("viewCase")
VIEW_CASES = _function_body("viewCases")


# --- the verdict tuple comes from the pointer row, in the renderer that draws it --------------

def test_case_detail_reads_the_pointer_row():
    assert "d.pointer_decision" in VIEW_CASE


def test_case_detail_never_picks_a_verdict_from_display_ordered_decisions():
    """`decisions[]` is decided_at-ordered for display; decided_at is transaction-start time and
    inverts against commit order, so it may never select the verdict."""
    assert "decisions[0]" not in VIEW_CASE
    assert not re.search(r"\bd\.decisions\s*\[", VIEW_CASE)


def test_case_detail_never_reads_the_stale_projection_column():
    assert "c.latest_decision" not in VIEW_CASE


# --- live evidence and the published decision are visibly different things --------------------

def test_case_detail_labels_live_evidence_as_not_the_decision():
    assert "current_evidence_score" in VIEW_CASE
    assert "Current Evidence Score" in VIEW_CASE


def test_case_detail_decision_card_sources_the_whole_tuple_from_one_row():
    """decision, score and buy enablement all read off `latest` (the pointed row) — never off
    the case projection or the live score (re-audit `cbb783b` F6)."""
    card = VIEW_CASE[VIEW_CASE.index("Decision Sent to Platform"):]
    card = card[: card.index("Current Evidence Score")]
    for expr in ("latest.decision", "latest.score", "latest.buy_enablement"):
        assert expr in card, f"the published-decision card must render {expr}"
    assert "Decision Rules" in card
    assert "published-gates" in card
    assert "gates[g0]" in card
    assert "current_score" not in card and "current_evidence_score" not in card
    assert '<h2 class="sec">Hard gates' not in VIEW_CASE


def test_live_bar_is_not_coloured_by_the_published_decision():
    """A neutral bar is the point: colouring live points by the published verdict merges two
    authority eras into one visual read.

    The neutral is asserted as a SET rather than one token. A cosmetic pass measured the old
    --faint bar at 3.10:1 against its track in light and 2.79:1 in dark, under the 3:1 floor for
    a meaningful graphic, and moved it to --muted (4.52:1 / 5.53:1). That is the same neutral
    register, so the guard's subject is unchanged; pinning the exact token made a contrast fix
    look like a semantics change. What must stay true is that the bar carries NO outcome hue --
    neither the published verdict's nor the live score's own, since a green bar beside an amber
    published verdict is the contradiction this screen already has too much of."""
    bar = VIEW_CASE[VIEW_CASE.index("bmeasure"):]
    bar = bar[: bar.index("btick")]
    assert any(n in bar for n in ("var(--muted)", "var(--faint)", "var(--text)")), bar
    for hue in ("decisionColor", "barColor", "green", "amber", "red", "success", "danger", "warning"):
        assert hue not in bar, f"the live bar must not carry an outcome hue: {hue}"


# --- the list shows both numbers, each under its own name ------------------------------------

def test_case_list_separates_decision_score_from_live_evidence():
    assert "c.decision_score" in VIEW_CASES
    assert "c.current_evidence_score" in VIEW_CASES
    # the blended field the list used to render under a single "Score" column
    assert "c.current_score" not in VIEW_CASES


# --- manual provenance is visible for every state (re-audit `8377440` F6) ---------------------

def test_manual_provenance_has_a_distinct_pill_for_every_state():
    """A legacy-unresolved manual history must not render identically to 'no manual approval'.
    All four API states need their own manual-scoped label in the pill map."""
    for state in ("latest_manual_row", "unresolved_pointer_drift",
                  "unresolved_legacy_order", "no_manual_decisions"):
        assert f"{state}_manual" in CONSOLE, f"manual provenance state {state!r} has no pill"


def test_case_detail_renders_the_manual_provenance_not_only_drift():
    """The full view must surface the manual provenance for every non-empty state, not just the
    drift case — otherwise `unresolved_legacy_order` is invisible (looks like no manual approval)."""
    assert 'd.manual_decision_provenance!=="no_manual_decisions"' in VIEW_CASE
    assert "provPill(d.manual_decision_provenance,true)" in VIEW_CASE


# --- the enforcement hold is the server's word, never inferred from green gates ---------------

def test_case_detail_takes_the_hold_from_the_api():
    """A computed approval the safety overlay held back is reported by the API
    (`enforcement_hold`, read from the engine's own run.decided record). The renderer shows that
    word inside the published-decision card; it never decides a case is held by testing the
    five gates against the threshold itself."""
    assert "d.enforcement_hold" in VIEW_CASE
    assert 'pill("held_for_approval")' in VIEW_CASE
    card = VIEW_CASE[VIEW_CASE.index("Decision Sent to Platform"):]
    card = card[: card.index("Current Evidence Score")]
    assert "holdCallout(hold" in card, "the hold panel lives inside the published-decision card"


def test_case_list_marks_held_approvals_from_the_api_flag():
    assert "c.enforcement_held" in VIEW_CASES
    assert 'pill("held_for_approval")' in VIEW_CASES


# --- approving by hand asks who and why, and the console never invents a reviewer -----------

def test_console_never_sends_a_canned_reviewer_identity():
    """`reviewer_id:"ops-console"` used to be hardcoded on the review-task buttons, so the
    audit row named a program. Every reviewer action now carries the id typed in the sidebar."""
    assert 'reviewer_id:"ops-console"' not in CONSOLE
    assert 'reviewer_id:"console"' not in CONSOLE
    assert "reviewer_id:reviewer()" in VIEW_CASE


def test_approve_by_hand_goes_through_the_dialog_and_requires_who_and_why():
    dialog = CONSOLE[CONSOLE.index('<dialog id="approve"'):]
    dialog = dialog[: dialog.index("</dialog>")]
    assert 'id="ap-who" required' in dialog
    assert 'id="ap-why" required' in dialog
    # the case renderer opens the dialog; it never posts an approval itself
    assert "openApprove(d,hold)" in VIEW_CASE
    assert "reviewer.manual_approve" not in VIEW_CASE


# --- the keyboard reaches the primary action, and a navigation is announced -------------------

def test_case_rows_carry_a_real_link():
    """Rows used to be click-only <tr>s: opening a case, the console's primary action, was
    mouse-only and could not be middle-clicked into a tab."""
    assert '<a href="#/case/${encodeURIComponent(c.id)}"' in VIEW_CASES


def test_shell_has_a_skip_link_and_a_focusable_main():
    assert '<a class="skip" href="#main"' in CONSOLE
    assert '<main id="main" tabindex="-1">' in CONSOLE


def test_router_marks_the_current_page_and_moves_focus_on_navigation():
    router = CONSOLE[CONSOLE.index("async function route()"):]
    assert 'setAttribute("aria-current","page")' in router
    assert "if(navigated)" in router and 't.focus({preventScroll:true})' in router


# --- the frame folds below 1024px, and wide content scrolls inside its own card --------------

def test_frame_folds_and_tables_scroll_in_their_cards():
    css = CONSOLE[: CONSOLE.index("</style>")]
    assert "@media(max-width:1023px)" in css
    assert ".card .bd.flush{padding:0;overflow-x:auto;" in css
    # the scroll box paints its own end-of-content shadows, so a clipped table is never silent
    assert "no-repeat scroll" in css
    assert '<button type="button" class="menubtn" id="menubtn" aria-controls="nav"' in CONSOLE


# --- the result reaches a reader who cannot see colour ----------------------------------------

def test_decision_rules_say_their_own_result_in_text():
    """The five gate cells name the SUBJECT ("Score threshold met") in both states and carried the
    outcome in the icon and the ground. Read aloud, a passed rule and a failed rule were the same
    sentence. The result is now text: hidden on a met rule, printed on a failed one."""
    card = VIEW_CASE[VIEW_CASE.index("published-gates"):]
    card = card[: card.index("Current Evidence Score")]
    assert '<span class="vh"> — met</span>' in card
    assert "— not met" in card
    assert ".vh{position:absolute" in CONSOLE, "the visually-hidden utility backing it is gone"


def test_evidence_rows_carry_their_status_as_text_and_hide_the_dot():
    """`.crow`'s status was a coloured dot whose `·` and `!` are TEXT, so the row announced
    '·Not Yet Available: Company email verified' — punctuation as content, and no status word."""
    assert 'sdot(it.status)}<span class="vh">${esc(say("status",it.status))}' in VIEW_CASE
    dots = VIEW_CASE[VIEW_CASE.index("const sdot="):]
    dots = dots[: dots.index("\n  const contrib")]
    assert dots.count('aria-hidden="true"') == 4, "every status dot is decoration"


# --- a rule written for one component may not reach another ----------------------------------

def test_the_inline_optical_correction_never_reaches_a_flex_centred_icon():
    """`td .ic` matched every icon inside a .pill/.iw/button.sm in a table cell. Those parents are
    inline-flex with align-items:center, where vertical-align is inert and the icon is already
    centred, so the -1.5px correction displaced 12 icons to fix 4."""
    css = CONSOLE[: CONSOLE.index("</style>")]
    rule = re.search(r"^([^\n]*)\{vertical-align:middle;position:relative;top:-1\.5px\}",
                     css, re.MULTILINE)
    assert rule, "the inline optical correction is gone"
    assert "td .ic" not in rule.group(1), rule.group(1)


def test_the_sidebar_uppercase_is_scoped_to_the_sidebar():
    """Unscoped, `.who` also matched `.fitem .who` — the activity feed's actor, whose own rule sets
    `font` but not case or tracking — so a reviewer id printed J.OKONJO in no token."""
    css = CONSOLE[: CONSOLE.index("</style>")]
    assert ".who-block .who{" in css
    assert not re.search(r"^\.who\{", css, re.MULTILINE)


def test_the_bypassed_gate_has_a_ground_of_its_own():
    """Fixed twice now: first from opacity .55 (~2.9:1 ink), then from a ground byte-identical to
    the card behind it, which left a 1.51:1 dashed edge as the only thing that made it a cell."""
    css = CONSOLE[: CONSOLE.index("</style>")]
    na = re.search(r"^\.gate\.na\{([^}]*)\}", css, re.MULTILINE)
    assert na, ".gate.na is gone"
    assert "background:var(--surface-muted)" in na.group(1)
    assert "var(--border-input)" in na.group(1), "the edge must take the 3:1 control token"
    assert "opacity" not in na.group(1)


# --- the header says each thing once ----------------------------------------------------------

def test_the_header_drops_a_chip_that_repeats_the_decision():
    """"On Hold" beside "Awaiting Further Evidence" is one enum under two vocabularies."""
    assert "decision&&c.status===decision" in VIEW_CASE
    assert 'd.manual_decision_provenance==="latest_manual_row"&&c.status==="approved_manual"' in VIEW_CASE


def test_every_pill_carries_its_raw_enum():
    """Rule 12 was held by the callers that happened to pass a label, not by the component."""
    fn = CONSOLE[CONSOLE.index("const pill=(k,label)=>"):]
    fn = fn[: fn.index("\n\n")]
    assert 'title="${esc(k)}"' in fn


def test_the_decision_history_prints_absolute_time():
    """It is read months later, when "5h ago" means nothing and four rounds say the same thing."""
    hist = VIEW_CASE[VIEW_CASE.index("Decision History"):] if "Decision History" in VIEW_CASE \
        else VIEW_CASE[VIEW_CASE.index("d.decisions.map"):]
    hist = hist[: hist.index("Activity Log")]
    assert "exact(x.decided_at)" in hist and "exact(x.published_at)" in hist
    assert "when(x.decided_at)" not in hist


def test_only_a_missing_value_renders_as_a_dash():
    """"—" means NOT SET. It must not also mean "set to false" for every field but one — the table
    used to special-case `Hard_Conflict__c` by name."""
    rows = VIEW_CASE[VIEW_CASE.index("const sfRows="):]
    rows = rows[: rows.index("const fitems=")]
    assert "v===null||v===undefined" in rows
    assert "Hard_Conflict__c" not in rows


def test_one_raised_button_per_screen():
    """Approving by hand is this screen's primary action; sending a test message is a utility. The
    elevation used to say the opposite."""
    assert '<button id="sendbtn">' in VIEW_CASE
    css = CONSOLE[: CONSOLE.index("</style>")]
    warn = re.search(r"^button\.warn\{([^}]*)\}", css, re.MULTILINE)
    assert warn and "box-shadow:var(--e2)" in warn.group(1)
    dialog = CONSOLE[CONSOLE.index('<dialog id="approve"'):]
    dialog = dialog[: dialog.index("</dialog>")]
    assert 'class="warn" id="ap-go"' in dialog, "the confirm keeps the trigger's colour"


def test_each_case_tab_is_named_for_its_company():
    assert "document.title=`${c.company_name||c.id} · KYC Tool`" in VIEW_CASE
