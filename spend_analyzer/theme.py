"""The framework seam: the Ethan design system mapped onto Streamlit, Plotly and
pandas Styler in one place.

The system itself (rules for colour, type, density, copy, iconography) lives in
``~/ethan_design_system`` — read its ``readme.md`` before changing a surface; token
values alone carry none of those rules. Views take colours and chart styling from
here and never write a hex of their own, and never branch on the theme: a token
read as ``theme.PAPER_0`` resolves against whichever theme the viewer has active.
``.streamlit/config.toml`` carries the values Streamlit's own widgets and canvas
tables need, per theme.
"""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

FONT_SERIF = '"Piazzolla", Georgia, "Times New Roman", serif'
FONT_SANS = '"Instrument Sans", "Helvetica Neue", Arial, sans-serif'
FONT_MONO = '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace'

# Ramp numbers are roles, not lightness: 900/700/600 is the foreground end and
# 050/100/200 the fill end in both themes.
#
# CATEGORICAL is a fixed order per theme, validated with the dataviz palette
# checker against that theme's PAPER_0 (lightness band, adjacent-pair CVD and
# normal-vision separation). Light azure sits a hair under the checker's chroma
# floor and stays because it is the system's info hue. A series past the last
# slot folds into OTHER rather than cycling.
_LIGHT = dict(
    PAPER_0="#FDFCF9", PAPER_1="#F8F6F1", PAPER_2="#F1EDE5",
    INK_900="#211D18", INK_700="#3E3830", INK_600="#574F45", INK_500="#70685B",
    INK_200="#D8D1C5", INK_100="#E8E2D8", INK_050="#F2EFE8",
    ORANGE_700="#944A17", ORANGE_500="#DD7A32", ORANGE_200="#F4D2AC", ORANGE_100="#FBE8D6",
    ACCENT_HOVER="#BF6321", ACCENT_PRESS="#944A17",
    JADE_500="#0E8A73", HAZARD_200="#C9C2FF", UNDER_FILL="#A9DCCF", SEQUENTIAL_FILL="#F4D2AC",
    INK_RULE="rgba(33,29,24,.14)", LINK_WASH="rgba(112,104,91,.22)",
    CATEGORICAL=["#DD7A32", "#1A7296", "#6B2E9E", "#0E8A73", "#A8321F", "#1F4FA8", "#8A6403"],
)
_DARK = dict(
    PAPER_0="#2B271F", PAPER_1="#15130E", PAPER_2="#0E0C09",
    INK_900="#F7F4ED", INK_700="#D6D0C5", INK_600="#B4AEA2", INK_500="#A59E90",
    INK_200="#504A3F", INK_100="#3A352C", INK_050="#35312A",
    ORANGE_700="#F6C08B", ORANGE_500="#DD7A32", ORANGE_200="#54453A", ORANGE_100="#403529",
    ACCENT_HOVER="#E68A42", ACCENT_PRESS="#C4682A",
    JADE_500="#33B98A", HAZARD_200="#4A2ADB", UNDER_FILL="#1C5A45", SEQUENTIAL_FILL="#7A5330",
    INK_RULE="rgba(247,244,237,.14)", LINK_WASH="rgba(165,158,144,.28)",
    CATEGORICAL=["#C4682A", "#2C86AD", "#B58A1A", "#9A63D0", "#1F9A76", "#4F7FD0", "#C8523D"],
)
# Roles a view asks for by meaning; each names the token it resolves to.
_ROLES = dict(INCOME="JADE_500", NET="INK_900", OTHER="INK_500", COMPARISON="INK_500")


def is_dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:  # noqa: BLE001 — no script run (imports, unit tests) ⇒ light
        return False


def _tokens() -> dict:
    return _DARK if is_dark() else _LIGHT


def __getattr__(name: str):
    tokens = _tokens()
    if name == "SPEND":
        return tokens["CATEGORICAL"][0]
    if name == "CELL_TEXT":
        return f"color: {tokens['INK_900']}"
    if name == "HIDDEN_ROW":
        return (f"color: {tokens['INK_500']}; font-style: italic; "
                f"background-color: {tokens['PAPER_2']}")
    key = _ROLES.get(name, name)
    if key in tokens:
        return tokens[key]
    raise AttributeError(f"module 'theme' has no token {name!r}")


def series_colors(names: list) -> dict:
    """Colour per series by position; everything past the palette shares OTHER."""
    tokens = _tokens()
    palette = tokens["CATEGORICAL"]
    return {n: (palette[i] if i < len(palette) else tokens["INK_500"])
            for i, n in enumerate(names)}


def _mix(a: str, b: str, t: float) -> str:
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(pa, pb))


def sequential(t: float) -> str:
    """Magnitude fill for a table cell, t in 0..1: card ground to the orange fill step."""
    tokens = _tokens()
    return _mix(tokens["PAPER_0"], tokens["SEQUENTIAL_FILL"], max(0.0, min(1.0, t)))


def diverging(t: float) -> str:
    """Polarity fill, t in -1..1: jade below the midpoint, hazard above, card ground at it."""
    tokens = _tokens()
    t = max(-1.0, min(1.0, t))
    return _mix(tokens["PAPER_0"], tokens["HAZARD_200" if t > 0 else "UNDER_FILL"], abs(t))


def _template(t: dict) -> go.layout.Template:
    axis = dict(gridcolor=t["INK_100"], linecolor=t["INK_200"], zerolinecolor=t["INK_200"],
                tickfont=dict(family=FONT_MONO, size=12, color=t["INK_600"]))
    return go.layout.Template(layout=dict(
        font=dict(family=FONT_SANS, size=13, color=t["INK_700"]),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        colorway=t["CATEGORICAL"],
        margin=dict(t=8, l=8, r=8, b=8),
        xaxis=axis, yaxis=axis,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, title_text="",
                    font=dict(size=12, color=t["INK_600"])),
        hoverlabel=dict(bgcolor=t["PAPER_0"], bordercolor=t["INK_200"],
                        font=dict(family=FONT_SANS, size=13, color=t["INK_900"])),
        bargap=0.28,
    ))


pio.templates["ethan-light"] = _template(_LIGHT)
pio.templates["ethan-dark"] = _template(_DARK)
pio.templates.default = "ethan-light"


def _root_vars(t: dict) -> str:
    return f"""
:root {{
  --paper-0: {t["PAPER_0"]}; --paper-1: {t["PAPER_1"]}; --paper-2: {t["PAPER_2"]};
  --text-heading: {t["INK_900"]}; --text-body: {t["INK_700"]}; --text-muted: {t["INK_600"]};
  --text-faint: {t["INK_500"]}; --border-subtle: {t["INK_100"]}; --border-default: {t["INK_200"]};
  --surface-hover: {t["INK_050"]}; --accent: {t["ORANGE_500"]}; --accent-hover: {t["ACCENT_HOVER"]};
  --accent-press: {t["ACCENT_PRESS"]}; --on-accent: #FFF8F1; --text-accent: {t["ORANGE_700"]};
  --orange-100: {t["ORANGE_100"]}; --orange-200: {t["ORANGE_200"]}; --ink-rule: {t["INK_RULE"]};
  --font-serif: {FONT_SERIF}; --font-sans: {FONT_SANS}; --font-mono: {FONT_MONO};
}}
"""


_CSS = """
<style>
.stApp, .stApp p, .stApp label, .stApp li, .stApp button, .stApp input, .stApp textarea,
.stApp [data-baseweb="select"], .stApp [data-baseweb="tab"] {
  font-family: var(--font-sans);
}
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {
  font-family: var(--font-serif); color: var(--text-heading); font-weight: 600;
  font-optical-sizing: auto; padding-bottom: 2px;
}
.stApp h1 { font-size: 28px; letter-spacing: -0.012em; padding-top: 0; }
.stApp h2 { font-size: 22px; letter-spacing: -0.009em; }
.stApp h3 { font-size: 18px; letter-spacing: -0.006em; padding: 6px 0 8px; line-height: 1.28; }
.stApp [data-testid="stHeadingWithActionElements"] { overflow: visible; }
.stApp code, .stApp [data-testid="stMetricValue"], .stApp [data-testid="stMetricValue"] * {
  font-family: var(--font-mono);
}
.stApp code { background: var(--paper-2); color: var(--text-body); border-radius: 3px; }

.stApp .block-container { padding: 46px 20px 32px; max-width: none; }
.stApp [data-testid="stVerticalBlock"] { gap: 9px; }
.stApp [data-testid="stHorizontalBlock"] { gap: 12px; }
section[data-testid="stSidebar"] {
  background: var(--paper-2); border-right: 1px solid var(--border-subtle);
}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] { gap: 8px; }

.stApp [data-testid="stMetric"] {
  background: var(--paper-0); border: 1px solid var(--border-subtle);
  border-radius: 10px; padding: 10px 12px;
}
.stApp [data-testid="stMetricLabel"] p {
  font-size: 12px; letter-spacing: 0.075em; text-transform: uppercase;
  color: var(--text-muted);
}
.stApp [data-testid="stMetricValue"] {
  font-size: 22px; font-weight: 500; color: var(--text-heading); line-height: 1.25;
}
.stApp [data-testid="stMetricDelta"] { font-family: var(--font-mono); font-size: 12px; }

.stApp button[kind="secondary"], .stApp button[kind="primary"],
.stApp [data-testid="stDownloadButton"] button {
  border-radius: 7px; min-height: 30px; padding: 2px 10px; font-weight: 500;
  border: 1px solid var(--border-default); transition: background 90ms;
}
.stApp button[kind="secondary"], .stApp [data-testid="stDownloadButton"] button {
  background: var(--paper-0); color: var(--text-body);
}
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stDownloadButton"] button:hover {
  background: var(--surface-hover); border-color: var(--border-default);
  color: var(--text-heading);
}
.stApp button[kind="primary"] {
  background: var(--accent); border-color: var(--accent); color: var(--on-accent);
}
.stApp button[kind="primary"]:hover {
  background: var(--accent-hover); border-color: var(--accent-hover);
}
.stApp button[kind="primary"]:active { background: var(--accent-press); }
.stApp button:disabled { opacity: .45; cursor: not-allowed; }

/* The view switcher is a radio dressed as the tab row. */
.st-key-view [role="radiogroup"] {
  gap: 2px; border-bottom: 1px solid var(--ink-rule); flex-wrap: wrap; width: 100%;
}
.st-key-view label[data-testid="stRadioOption"] {
  height: 36px; padding: 0 10px; margin: 0 0 -1px; cursor: pointer;
  border-bottom: 2px solid transparent; border-radius: 5px 5px 0 0;
  display: flex; align-items: center;
}
.st-key-view label[data-testid="stRadioOption"] > div > div:first-child { display: none; }
.st-key-view label[data-testid="stRadioOption"] p {
  font-size: 15px; font-weight: 500; color: var(--text-muted);
}
.st-key-view label[data-testid="stRadioOption"]:hover { background: var(--surface-hover); }
.st-key-view label[data-testid="stRadioOption"][data-selected="true"] {
  border-bottom-color: var(--accent);
}
.st-key-view label[data-testid="stRadioOption"][data-selected="true"] p {
  color: var(--text-heading);
}

.stApp [data-baseweb="tab-list"] { gap: 2px; border-bottom: 1px solid var(--ink-rule); }
.stApp [data-baseweb="tab"] { height: 36px; padding: 0 10px; color: var(--text-muted); }
.stApp [data-baseweb="tab"][aria-selected="true"] { color: var(--text-heading); }
.stApp [data-baseweb="tab"] p { font-size: 15px; font-weight: 500; }

.stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"],
.stApp [data-baseweb="base-input"] { border-radius: 7px; }
.stApp [data-testid="stExpander"] details {
  background: var(--paper-0); border: 1px solid var(--border-subtle); border-radius: 10px;
}
.stApp [data-testid="stCaptionContainer"] p { color: var(--text-muted); font-size: 14px; }
.stApp hr { border-color: var(--ink-rule); margin: 8px 0; }
.stApp a { color: var(--text-accent); text-underline-offset: 2px; }

/* st.dataframe is drag-resizable from its bottom-right corner. */
.stApp div[data-testid="stDataFrame"] {
  resize: horizontal; overflow: auto; min-width: 280px;
  border: 1px solid var(--border-subtle); border-radius: 10px;
}

.eyebrow {
  padding: 4px 0 2px;
  font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.075em;
  text-transform: uppercase; color: var(--text-muted); margin: 0;
}
.marker { background: linear-gradient(transparent 62%, var(--orange-200) 62%); }

@media (max-width: 820px) {
  div[data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
    min-width: 100% !important; flex: 1 1 100% !important;
  }
}
</style>
"""


def apply() -> None:
    tokens = _tokens()
    pio.templates.default = "ethan-dark" if is_dark() else "ethan-light"
    st.markdown(f"<style>{_root_vars(tokens)}</style>" + _CSS, unsafe_allow_html=True)


def eyebrow_in(container, text: str) -> None:
    """The uppercase micro label that heads a section where an h3 would be too loud."""
    container.markdown(f'<p class="eyebrow">{text}</p>', unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    eyebrow_in(st, text)
