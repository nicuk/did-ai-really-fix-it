"""Generate the README visuals for Cairn Verify in the Cairn brand palette.

Shared with Cairn Memory (same palette, pebbles, hero and terminal layout); only the
words, the terminal transcript and the diagram differ. Run: python make_assets.py assets

Every animation plays once and ends on a readable frame; motion is dropped for
readers who ask for reduced motion, and the resting state is the visible one.
Light cards to match the logo, which is drawn on white; the terminal stays dark.
"""
from pathlib import Path
from xml.sax.saxutils import escape
import sys

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

# brand, sampled from the Cairn logo
INK = "#111827"        # wordmark
BLUE = "#0A6CFF"
SKY = "#6CCBFF"
DEEP = "#16255C"
DIM = "#5B6B85"
CARD = "#FFFFFF"
CARD2 = "#F6F9FF"
EDGE = "#DCE5F5"
GREEN = "#16A34A"
AMBER = "#D97706"
# terminal
T_BG, T_BAR, T_TEXT, T_DIM = "#0F172A", "#1E293B", "#E2E8F0", "#94A3B8"
T_BLUE, T_GREEN, T_RED, T_AMBER = "#60A5FA", "#4ADE80", "#F87171", "#FBBF24"

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
REDUCED = "@media (prefers-reduced-motion: reduce) { * { animation: none !important; } }"

# The three pebbles of the mark, in the logo's own 1254px coordinate space.
PEBBLES = [
    ("bottom", "M252,915 C255,840 360,770 520,735 C660,705 820,700 920,745 "
               "C1000,780 1015,900 960,985 C900,1045 700,1045 560,1042 C400,1040 250,1010 252,915 Z",
     [("0", "#12204F"), ("0.55", "#1B3A8C"), ("1", "#2366E0")], (0, 1, 1, 0)),
    ("middle", "M315,660 C310,560 390,508 520,506 C650,504 820,520 880,570 "
               "C930,615 880,690 760,720 C650,748 520,760 430,748 C360,740 318,705 315,660 Z",
     [("0", "#1E90FF"), ("1", "#0050E6")], (0, 0, 1, 1)),
    ("top", "M440,430 C430,360 560,270 700,248 C790,235 820,320 790,390 "
            "C760,460 690,500 590,500 C500,500 445,470 440,430 Z",
     [("0", "#7DD6FF"), ("1", "#0A74FF")], (0.6, 0, 0.4, 1)),
]


def hero() -> str:
    defs, stones = [], []
    for i, (name, d, stops, (x1, y1, x2, y2)) in enumerate(PEBBLES):
        s = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in stops)
        defs.append(f'<linearGradient id="g{name}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">{s}</linearGradient>')
        stones.append(f'<g class="drop" style="animation-delay:{0.15 + i * 0.35:.2f}s">'
                      f'<path d="{d}" fill="url(#g{name})"/></g>')
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 400" width="1280" height="400" role="img" aria-label="Three stones stack into a cairn: did the AI really fix it?">
<style>
.drop {{ animation: drop .7s cubic-bezier(.2,.9,.3,1.25) both; }}
@keyframes drop {{ from {{ transform: translateY(-900px); opacity: 0; }} 60% {{ opacity: 1; }} to {{ transform: none; opacity: 1; }} }}
.glow {{ animation: glow 3.4s ease-in-out 1.6s infinite; }}
@keyframes glow {{ 0%,100% {{ opacity: 0; }} 50% {{ opacity: .45; }} }}
.fade {{ animation: fade .9s ease-out both; }}
@keyframes fade {{ from {{ opacity: 0; transform: translateX(-14px); }} to {{ opacity: 1; transform: none; }} }}
{REDUCED}
</style>
<defs>{''.join(defs)}<filter id="blur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="40"/></filter></defs>
<rect x="1" y="1" width="1278" height="398" rx="28" fill="{CARD}" stroke="{EDGE}" stroke-width="2"/>
<g transform="translate(40 -40) scale(0.4)">
  <ellipse class="glow" cx="620" cy="380" rx="220" ry="120" fill="{SKY}" opacity="0" filter="url(#blur)"/>
  {''.join(stones)}
</g>
<g class="fade" style="animation-delay:1.2s">
  <text x="480" y="170" font-family="{SANS}" font-size="46" font-weight="700" fill="{INK}">Did the AI</text>
  <text x="480" y="226" font-family="{SANS}" font-size="46" font-weight="700" fill="{BLUE}">really fix it?</text>
</g>
<g class="fade" style="animation-delay:1.6s">
  <text x="482" y="282" font-family="{SANS}" font-size="22" fill="{DIM}">Every claim your coding agent made, checked against the repo.</text>
  <text x="482" y="314" font-family="{SANS}" font-size="22" fill="{DIM}">Holds, doesn't hold, or can't tell, in words a founder can act on.</text>
</g>
</svg>
"""


def terminal() -> str:
    # Real verify_claims.py output (2026-09-27) on a small made-up app whose "agent" commits
    # overclaim. Three of the thirteen claims are shown; long lines are shortened with "…".
    P, F, W, G, D, T = "prompt", "fail", "warn", "ok", "dim", "text"
    lines = [
        (P, "$ python verify_claims.py --self-test"),
        (G, "ok   removed-still-referenced"),
        (G, "ok   twin"),
        (D, "…    53 more checks, each forced to fail once on a planted defect"),
        (G, "self-test passed: 55/55 checks fired"),
        (T, ""),
        (P, "$ python verify_claims.py claims --repo app-repo --range base..main --text agent-summary.txt"),
        (F, "[CONTRADICTED] (summary) Wired audit logging into checkout, so it's now live on every checkout."),
        (D, "  - wired-but-unimported: app/audit.py was added in this range, and nothing outside tests imports it"),
        (W, "[UNPROVEN] (ffcb5ae) No behaviour change."),
        (D, "  - behaviour-claim-tests-edited: existing assertions were edited in tests/test_billing.py; …"),
        (W, "[UNPROVEN] (summary) Added tests for token expiry, and all 7 tests pass."),
        (D, "  - test-selection-changed: tests/__init__.py changed which tests run (`def load_tests(…)`); …"),
        (T, ""),
        (P, "$ python verify_claims.py orphans --repo app-repo"),
        (T, "kept alive only by tests (1): tests import these, nothing a user reaches does"),
        (T, "  app/legacy/session.py"),
        (F, "twin `create_session`: live in app/auth/session.py; not reachable in app/legacy/session.py"),
    ]
    color = {P: T_BLUE, F: T_RED, W: T_AMBER, G: T_GREEN, D: T_DIM, T: T_TEXT}
    weight = {F: "700", W: "700"}
    y0, lh = 96, 30
    rows, t = [], 0.4
    for i, (kind, s) in enumerate(lines):
        y = y0 + i * lh
        if kind == P:
            width = int(len(s) * 11.1) + 12
            dur = max(0.6, len(s) * 0.035)
            steps = ";".join(str(int(width * k / 24)) for k in range(25))
            rows.append(
                f'<clipPath id="c{i}"><rect x="36" y="{y - 22}" height="30" width="{width}">'
                f'<animate attributeName="width" begin="{t:.2f}s" dur="{dur:.2f}s" calcMode="discrete" '
                f'values="{steps}" fill="freeze"/></rect></clipPath>'
                f'<text x="40" y="{y}" clip-path="url(#c{i})" font-family="{MONO}" font-size="19" fill="{color[kind]}">{escape(s)}</text>')
            t += dur + 0.35
        else:
            rows.append(
                f'<text class="line" style="animation-delay:{t:.2f}s" x="40" y="{y}" font-family="{MONO}" '
                f'font-size="19" font-weight="{weight.get(kind, "400")}" fill="{color[kind]}" xml:space="preserve">{escape(s)}</text>')
            t += 0.16 if s else 0.3
    h = y0 + len(lines) * lh + 20
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 {h}" width="1280" height="{h}" role="img" aria-label="The script's self-test firing all 55 checks, then a claims check that contradicts an unwired audit log and questions a behaviour claim and a test count, then an orphan scan that finds the session fix went into a copy only tests reach">
<style>
.line {{ animation: in .25s ease-out both; }}
@keyframes in {{ from {{ opacity: 0; transform: translateY(4px); }} to {{ opacity: 1; transform: none; }} }}
{REDUCED}
</style>
<rect width="1280" height="{h}" rx="18" fill="{T_BG}"/>
<rect width="1280" height="44" rx="18" fill="{T_BAR}"/>
<rect y="26" width="1280" height="18" fill="{T_BAR}"/>
<circle cx="30" cy="22" r="7" fill="#F87171"/><circle cx="54" cy="22" r="7" fill="#FBBF24"/><circle cx="78" cy="22" r="7" fill="#4ADE80"/>
<text x="640" y="28" text-anchor="middle" font-family="{SANS}" font-size="15" fill="{T_DIM}">verify_claims.py — read-only, no network</text>
{''.join(rows)}
</svg>
"""


def layers() -> str:
    # The script's checks, grouped by what they catch.
    cols = [
        ("Said it, didn't do it", "the diff contradicts the sentence", BLUE, [
            ("file-not-in-diff", "'fixed x.ts', and x.ts never changed"),
            ("tests-claimed-none-changed", "'added tests', and no test changed"),
            ("removed-still-referenced", "deleted a file something still imports"),
            ("wired-but-unimported", "'wired it up', and nothing calls it"),
        ]),
        ("Green, but proves nothing", "tests that can't fail for the right reason", SKY, [
            ("tests-never-import-changed", "the tests import none of the changed code"),
            ("skip-added", "a skip or only added to get to green"),
            ("test-selection-changed", "a config quietly drops a test"),
            ("behaviour-claim-tests-edited", "'no behaviour change', assertions edited"),
        ]),
        ("Fixing the wrong copy", "why the agent goes in circles", AMBER, [
            ("orphan rounds", "dead code kept alive by other dead code"),
            ("twin", "two files, one name: which one runs"),
            ("kept alive only by tests", "a green test on code no user reaches"),
        ]),
    ]
    colw, gap, x0, top = 384, 24, 40, 150
    out, n = [], 0
    for c, (title, sub, col, items) in enumerate(cols):
        x = x0 + c * (colw + gap)
        out.append(
            f'<g class="pop" style="animation-delay:{0.2 + c * 0.25:.2f}s">'
            f'<rect x="{x}" y="{top - 70}" width="{colw}" height="5" rx="2.5" fill="{col}"/>'
            f'<text x="{x}" y="{top - 36}" font-family="{SANS}" font-size="24" font-weight="700" fill="{INK}">{escape(title)}</text>'
            f'<text x="{x}" y="{top - 10}" font-family="{SANS}" font-size="16" fill="{DIM}">{escape(sub)}</text></g>')
        for r, (name, what) in enumerate(items):
            y = top + 14 + r * 92
            out.append(
                f'<g class="pop" style="animation-delay:{0.9 + n * 0.12:.2f}s">'
                f'<rect x="{x}" y="{y}" width="{colw}" height="78" rx="39" fill="{CARD}" stroke="{col}" stroke-opacity=".6" stroke-width="2"/>'
                f'<text x="{x + 30}" y="{y + 34}" font-family="{MONO}" font-size="18" font-weight="600" fill="{INK}">{escape(name)}</text>'
                f'<text x="{x + 30}" y="{y + 60}" font-family="{SANS}" font-size="15" fill="{DIM}">{escape(what)}</text></g>')
            n += 1
    h = top + 14 + 4 * 92 + 40
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 {h}" width="1280" height="{h}" role="img" aria-label="What the script checks: claims the diff contradicts, tests that prove nothing, and fixes made in the wrong copy">
<style>
.pop {{ animation: pop .45s cubic-bezier(.2,.9,.3,1.2) both; }}
@keyframes pop {{ from {{ opacity: 0; transform: translateY(10px); }} to {{ opacity: 1; transform: none; }} }}
{REDUCED}
</style>
<rect x="1" y="1" width="1278" height="{h - 2}" rx="28" fill="{CARD2}" stroke="{EDGE}" stroke-width="2"/>
<text x="40" y="52" font-family="{SANS}" font-size="15" font-weight="700" letter-spacing="2" fill="{BLUE}">WHAT IT CHECKS</text>
{''.join(out)}
</svg>
"""


def icon() -> str:
    """The plugin icon: the Cairn mark on white, as a vector.

    Written to .claude-plugin/icon.svg, which the directory picks up by file name, so
    plugin.json needs no `icon` field (the field drew an UNKNOWN_KEY_CROSS_TOOL warning).
    Matches the logo's mark: the upper stones lifted to leave a gap, and the base stone
    two-toned by a darker band clipped to its outline.
    """
    p = {name: (d, stops, v) for name, d, stops, v in PEBBLES}
    lift = {"middle": -34, "top": -62}

    def grad(gid, stops, v):
        x1, y1, x2, y2 = v
        s = "".join(f'<stop offset="{o}" stop-color="{c}"/>' for o, c in stops)
        return f'<linearGradient id="{gid}" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}">{s}</linearGradient>'

    shade = "M200,1060 L200,820 C330,760 470,760 560,800 C680,852 790,960 1000,1000 L1000,1080 Z"
    defs = (grad("ib", [("0", "#1E4FB8"), ("1", "#2A6BE6")], (0, 1, 1, 0))
            + grad("is", [("0", "#0E1838"), ("1", "#1A2E6E")], (0, 0, 1, 1))
            + grad("im", *p["middle"][1:]) + grad("it", *p["top"][1:])
            + f'<clipPath id="cb"><path d="{p["bottom"][0]}"/></clipPath>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="22 -15 1220 1220" width="512" height="512" role="img" aria-label="Cairn">'
            f'<defs>{defs}</defs><rect x="22" y="-15" width="1220" height="1220" fill="#FFFFFF"/>'
            f'<path d="{p["bottom"][0]}" fill="url(#ib)"/>'
            f'<path d="{shade}" fill="url(#is)" clip-path="url(#cb)"/>'
            f'<path d="{p["middle"][0]}" fill="url(#im)" transform="translate(0 {lift["middle"]})"/>'
            f'<path d="{p["top"][0]}" fill="url(#it)" transform="translate(0 {lift["top"]})"/>'
            '</svg>' + chr(10))


for name, fn in [("hero.svg", hero), ("verify-demo.svg", terminal), ("checks.svg", layers)]:
    (OUT / name).write_text(fn(), encoding="utf-8")
    print(name, len((OUT / name).read_bytes()), "bytes")

ICON_PATH = Path(__file__).resolve().parent.parent / ".claude-plugin" / "icon.svg"
ICON_PATH.write_text(icon(), encoding="utf-8")
print("icon.svg", len(ICON_PATH.read_bytes()), "bytes")
