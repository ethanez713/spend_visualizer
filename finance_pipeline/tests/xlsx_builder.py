"""Write a minimal valid .xlsx for tests: ``{sheet name: [[cell, ...], ...]}``.

A cell is ``None`` (empty), ``str`` (shared string), ``float``/``int`` (number),
``date`` (stored as an Excel serial), or ``Rich(...)``/``Inline(...)`` to exercise the
other text encodings real exports use.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import date
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class Rich:
    pieces: tuple[str, ...]


@dataclass(frozen=True)
class Inline:
    text: str


def _col(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def make_xlsx(path, sheets: dict[str, list[list]], *, reverse_part_order: bool = False):
    strings: list[str] = []

    def shared(item) -> int:
        if item not in strings:
            strings.append(item)
        return strings.index(item)

    parts = {}
    for n, (name, grid) in enumerate(sheets.items(), start=1):
        rows = []
        for r, cells in enumerate(grid, start=1):
            out = []
            for c, cell in enumerate(cells):
                ref = f"{_col(c)}{r}"
                if cell is None:
                    continue
                if isinstance(cell, Inline):
                    out.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(cell.text)}</t></is></c>')
                elif isinstance(cell, Rich):
                    out.append(f'<c r="{ref}" t="s"><v>{shared(cell)}</v></c>')
                elif isinstance(cell, str):
                    out.append(f'<c r="{ref}" t="s"><v>{shared(cell)}</v></c>')
                elif isinstance(cell, date):
                    out.append(f'<c r="{ref}"><v>{(cell - date(1899, 12, 30)).days}</v></c>')
                else:
                    out.append(f'<c r="{ref}"><v>{cell}</v></c>')
            rows.append(f'<row r="{r}">{"".join(out)}</row>')
        parts[n] = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                    f'spreadsheetml/2006/main"><sheetData>{"".join(rows)}</sheetData></worksheet>')

    items = []
    for s in strings:
        if isinstance(s, Rich):
            runs = "".join(f"<r><t>{escape(p)}</t></r>" for p in s.pieces)
            items.append(f"<si>{runs}<rPh><t>ignored</t></rPh></si>")
        else:
            items.append(f"<si><t>{escape(s)}</t></si>")
    names = list(sheets)
    numbering = list(reversed(range(1, len(names) + 1))) if reverse_part_order else list(
        range(1, len(names) + 1))
    sheet_tags = "".join(
        f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(names, 1))
    rel_tags = "".join(
        f'<Relationship Id="rId{i}" Type="worksheet" Target="worksheets/sheet{numbering[i - 1]}.xml"/>'
        for i in range(1, len(names) + 1))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                   f'officeDocument/2006/relationships"><sheets>{sheet_tags}</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                   f'package/2006/relationships">{rel_tags}</Relationships>')
        z.writestr("xl/sharedStrings.xml",
                   '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
                   f'spreadsheetml/2006/main">{"".join(items)}</sst>')
        for i, n in enumerate(names, 1):
            z.writestr(f"xl/worksheets/sheet{numbering[i - 1]}.xml", parts[i])
