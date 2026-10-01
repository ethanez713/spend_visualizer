"""The framework seam: the Ethan design system mapped onto Streamlit, Plotly and
pandas Styler in one place.

The system itself (rules for colour, type, density, copy, iconography) lives in
``~/ethan_design_system`` — read its ``readme.md`` before changing a surface; token
values alone carry none of those rules. Views take colours and chart styling from
here and never write a hex of their own. ``.streamlit/config.toml`` carries the
four values Streamlit's own theme needs (canvas tables read only those).
"""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

PAPER_0, PAPER_1, PAPER_2 = "#FDFCF9", "#F8F6F1", "#F1EDE5"
INK_900, INK_700, INK_600, INK_500 = "#211D18", "#3E3830", "#574F45", "#70685B"
INK_200, INK_100, INK_050 = "#D8D1C5", "#E8E2D8", "#F2EFE8"
ORANGE_700, ORANGE_500, ORANGE_200, ORANGE_100 = "#944A17", "#DD7A32", "#F4D2AC", "#FBE8D6"
JADE_600, JADE_500, JADE_100 = "#0B6E5D", "#0E8A73", "#DCF0EB"
AZURE_500 = "#1A7296"
HAZARD_600, HAZARD_200, HAZARD_100 = "#3A16F5", "#C9C2FF", "#E8E5FF"
INK_RULE = "rgba(33,29,24,.14)"

FONT_SERIF = '"Piazzolla", Georgia, "Times New Roman", serif'
FONT_SANS = '"Instrument Sans", "Helvetica Neue", Arial, sans-serif'
FONT_MONO = '"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace'

# Fixed categorical order, validated with the dataviz palette checker against
# PAPER_0 (adjacent-pair CVD and normal-vision separation pass). Azure sits a hair
# under the checker's chroma floor and stays because it is the system's info hue.
# A series past the last slot folds into OTHER rather than cycling.
CATEGORICAL = [ORANGE_500, AZURE_500, "#6B2E9E", JADE_500, "#A8321F", "#1F4FA8", "#8A6403"]
OTHER = INK_500

INCOME, SPEND, NET = JADE_500, ORANGE_500, INK_900
COMPARISON = INK_500   # the reference series a current period is read against


def series_colors(names: list) -> dict:
    """Colour per series by position; everything past the palette shares OTHER."""
    return {n: (CATEGORICAL[i] if i < len(CATEGORICAL) else OTHER)
            for i, n in enumerate(names)}


def _mix(a: str, b: str, t: float) -> str:
    pa, pb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(pa, pb))


def sequential(t: float) -> str:
    """Magnitude fill for a table cell, t in 0..1: paper to the soft orange step."""
    return _mix(PAPER_0, ORANGE_200, max(0.0, min(1.0, t)))


def diverging(t: float) -> str:
    """Polarity fill, t in -1..1: jade below the midpoint, hazard above, paper at it."""
    t = max(-1.0, min(1.0, t))
    return _mix(PAPER_0, HAZARD_200 if t > 0 else "#A9DCCF", abs(t))


CELL_TEXT = f"color: {INK_900}"
HIDDEN_ROW = f"color: {INK_500}; font-style: italic; background-color: {PAPER_2}"

_TEMPLATE = go.layout.Template(layout=dict(
    font=dict(family=FONT_SANS, size=13, color=INK_700),
    paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
    colorway=CATEGORICAL,
    margin=dict(t=8, l=8, r=8, b=8),
    xaxis=dict(gridcolor=INK_100, linecolor=INK_200, zerolinecolor=INK_200,
               tickfont=dict(family=FONT_MONO, size=12, color=INK_600)),
    yaxis=dict(gridcolor=INK_100, linecolor=INK_200, zerolinecolor=INK_200,
               tickfont=dict(family=FONT_MONO, size=12, color=INK_600)),
    legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0, title_text="",
                font=dict(size=12, color=INK_600)),
    hoverlabel=dict(bgcolor=PAPER_0, bordercolor=INK_200,
                    font=dict(family=FONT_SANS, size=13, color=INK_900)),
    bargap=0.28,
))
pio.templates["ethan"] = _TEMPLATE
pio.templates.default = "ethan"

_CSS = f"""
<style>
@font-face {{ font-family: "Piazzolla"; font-weight: 400 700; font-display: swap;
  src: url("app/static/fonts/Piazzolla-400-700.woff2") format("woff2"); }}
@font-face {{ font-family: "Instrument Sans"; font-weight: 400 700; font-display: swap;
  src: url("app/static/fonts/InstrumentSans-400-700.woff2") format("woff2"); }}
@font-face {{ font-family: "IBM Plex Mono"; font-weight: 400; font-display: swap;
  src: url("app/static/fonts/IBMPlexMono-400.woff2") format("woff2"); }}
@font-face {{ font-family: "IBM Plex Mono"; font-weight: 500; font-display: swap;
  src: url("app/static/fonts/IBMPlexMono-500.woff2") format("woff2"); }}

:root {{
  --paper-0: {PAPER_0}; --paper-1: {PAPER_1}; --paper-2: {PAPER_2};
  --text-heading: {INK_900}; --text-body: {INK_700}; --text-muted: {INK_600};
  --text-faint: {INK_500}; --border-subtle: {INK_100}; --border-default: {INK_200};
  --surface-hover: {INK_050}; --accent: {ORANGE_500}; --accent-hover: #BF6321;
  --accent-press: {ORANGE_700}; --on-accent: #FFF8F1; --text-accent: {ORANGE_700};
  --orange-100: {ORANGE_100}; --orange-200: {ORANGE_200}; --ink-rule: {INK_RULE};
  --font-serif: {FONT_SERIF}; --font-sans: {FONT_SANS}; --font-mono: {FONT_MONO};
}}

.stApp, .stApp p, .stApp label, .stApp li, .stApp button, .stApp input, .stApp textarea,
.stApp [data-baseweb="select"], .stApp [data-baseweb="tab"] {{
  font-family: var(--font-sans);
}}
.stApp h1, .stApp h2, .stApp h3, .stApp h4 {{
  font-family: var(--font-serif); color: var(--text-heading); font-weight: 600;
  font-optical-sizing: auto; padding-bottom: 2px;
}}
.stApp h1 {{ font-size: 28px; letter-spacing: -0.012em; padding-top: 0; }}
.stApp h2 {{ font-size: 22px; letter-spacing: -0.009em; }}
.stApp h3 {{ font-size: 18px; letter-spacing: -0.006em; padding-top: 6px; }}
.stApp code, .stApp [data-testid="stMetricValue"] {{ font-family: var(--font-mono); }}
.stApp code {{ background: var(--paper-2); color: var(--text-body); border-radius: 3px; }}

.stApp .block-container {{ padding: 46px 20px 32px; max-width: none; }}
.stApp [data-testid="stVerticalBlock"] {{ gap: 9px; }}
.stApp [data-testid="stHorizontalBlock"] {{ gap: 12px; }}
section[data-testid="stSidebar"] {{
  background: var(--paper-2); border-right: 1px solid var(--border-subtle);
}}
section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{ gap: 8px; }}

.stApp [data-testid="stMetric"] {{
  background: var(--paper-0); border: 1px solid var(--border-subtle);
  border-radius: 10px; padding: 10px 12px;
}}
.stApp [data-testid="stMetricLabel"] p {{
  font-size: 12px; letter-spacing: 0.075em; text-transform: uppercase;
  color: var(--text-muted);
}}
.stApp [data-testid="stMetricValue"] {{
  font-size: 22px; font-weight: 500; color: var(--text-heading); line-height: 1.25;
}}
.stApp [data-testid="stMetricDelta"] {{ font-family: var(--font-mono); font-size: 12px; }}

.stApp button[kind="secondary"], .stApp button[kind="primary"],
.stApp [data-testid="stDownloadButton"] button {{
  border-radius: 7px; min-height: 30px; padding: 2px 10px; font-weight: 500;
  border: 1px solid var(--border-default); transition: background 90ms;
}}
.stApp button[kind="secondary"], .stApp [data-testid="stDownloadButton"] button {{
  background: var(--paper-0); color: var(--text-body);
}}
.stApp button[kind="secondary"]:hover, .stApp [data-testid="stDownloadButton"] button:hover {{
  background: var(--surface-hover); border-color: var(--border-default);
  color: var(--text-heading);
}}
.stApp button[kind="primary"] {{
  background: var(--accent); border-color: var(--accent); color: var(--on-accent);
}}
.stApp button[kind="primary"]:hover {{
  background: var(--accent-hover); border-color: var(--accent-hover);
}}
.stApp button[kind="primary"]:active {{ background: var(--accent-press); }}
.stApp button:disabled {{ opacity: .45; cursor: not-allowed; }}

.stApp [data-baseweb="tab-list"] {{ gap: 2px; border-bottom: 1px solid var(--ink-rule); }}
.stApp [data-baseweb="tab"] {{ height: 36px; padding: 0 10px; color: var(--text-muted); }}
.stApp [data-baseweb="tab"][aria-selected="true"] {{ color: var(--text-heading); }}
.stApp [data-baseweb="tab"] p {{ font-size: 15px; font-weight: 500; }}

.stApp [data-baseweb="select"] > div, .stApp [data-baseweb="input"],
.stApp [data-baseweb="base-input"] {{ border-radius: 7px; }}
.stApp [data-testid="stExpander"] details {{
  background: var(--paper-0); border: 1px solid var(--border-subtle); border-radius: 10px;
}}
.stApp [data-testid="stCaptionContainer"] p {{ color: var(--text-muted); font-size: 14px; }}
.stApp hr {{ border-color: var(--ink-rule); margin: 8px 0; }}
.stApp a {{ color: var(--text-accent); text-underline-offset: 2px; }}

/* st.dataframe is drag-resizable from its bottom-right corner. */
.stApp div[data-testid="stDataFrame"] {{
  resize: horizontal; overflow: auto; min-width: 280px;
  border: 1px solid var(--border-subtle); border-radius: 10px;
}}

.eyebrow {{
  font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.075em;
  text-transform: uppercase; color: var(--text-muted); margin: 0;
}}
.marker {{ background: linear-gradient(transparent 62%, var(--orange-200) 62%); }}

@media (max-width: 820px) {{
  div[data-testid="stHorizontalBlock"] {{ flex-wrap: wrap; }}
  div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {{
    min-width: 100% !important; flex: 1 1 100% !important;
  }}
}}
</style>
"""


def apply() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def eyebrow_in(container, text: str) -> None:
    """The uppercase micro label that heads a section where an h3 would be too loud."""
    container.markdown(f'<p class="eyebrow">{text}</p>', unsafe_allow_html=True)


def eyebrow(text: str) -> None:
    eyebrow_in(st, text)
