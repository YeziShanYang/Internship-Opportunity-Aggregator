"""Regenerate out/programs.xlsx from the CSVs (spec section 4).

The xlsx is a build artifact, never hand-edited. Git treats a spreadsheet as an opaque
zip of XML, so committing one as the source of truth would make every run look like the
whole file changed and destroy the audit trail. The CSVs are canonical; this script
renders them, preserving the seed file's formatting: Arial, a colour-coded eligibility
column, autofilter, frozen panes and a Legend sheet.

Four sheets:
  Programs     - every tracked program, canonical
  Legend       - what the codes mean
  Manual Watch - what this tool provably cannot tell you about, and when to look
  Priority     - the ranked shortlist to watch by hand as a backstop
"""
from __future__ import annotations

import csv
import pathlib
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import state

ARIAL = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=ARIAL, size=11, bold=True, color="FFFFFF")
HEADER_ALIGN = Alignment(horizontal="left", vertical="center", wrap_text=True)
BODY_FONT = Font(name=ARIAL, size=10)
BODY_ALIGN = Alignment(vertical="top", wrap_text=True)
LINK_FONT = Font(name=ARIAL, size=10, color="0563C1", underline="single")

# Exactly the palette from the seed spreadsheet.
GREEN = (PatternFill("solid", fgColor="C6EFCE"), "006100")
RED = (PatternFill("solid", fgColor="FFC7CE"), "9C0006")
AMBER = (PatternFill("solid", fgColor="FFEB9C"), "9C6500")
BLUE = (PatternFill("solid", fgColor="DDEBF7"), "1F4E79")

ELIGIBLE_STYLES = {
    "YES": GREEN,
    "USE": GREEN,
    "USE WEEKLY": GREEN,
    "NO": RED,
    "STALE": RED,
    "LATER": AMBER,
    "LOW VALUE": AMBER,
    "APPLIED - too early": AMBER,
    "CHECK": BLUE,
    "INVITE": BLUE,
    "VIA CLUB": BLUE,
}

PROGRAM_HEADERS = [
    ("category", "Category", 20),
    ("name", "Name", 44),
    ("website", "Website", 56),
    ("applications_open", "Applications Open", 30),
    ("target_years", "Target Years in College", 30),
    ("eligible", "Eligible for You", 17),
    ("notes", "Notes", 78),
    ("source_id", "Source ID", 22),
    ("status", "Status", 12),
    ("last_checked", "Last Checked", 22),
    ("last_changed", "Last Changed", 22),
    ("first_seen", "First Seen", 14),
    ("muted", "Muted", 8),
    ("snapshot_hash", "Snapshot Hash", 18),
]

MANUAL_HEADERS = [
    ("name", "Name", 44),
    ("category", "Category", 20),
    ("coverage", "Will this tool cover it?", 26),
    ("why_manual", "Why it is manual", 76),
    ("how_to_check", "How to check", 54),
    ("when_to_check", "When to check", 46),
    ("url", "URL", 56),
]

PRIORITY_HEADERS = [
    ("rank", "#", 5),
    ("band", "Band", 34),
    ("name", "Name", 46),
    ("category", "Category", 20),
    ("value", "Value", 13),
    ("probability", "Odds", 13),
    ("watch_window", "Watch window", 38),
    ("will_this_tool_alert_you", "Will this tool alert you?", 30),
    ("why", "Why it earns the slot", 96),
    ("url", "URL", 52),
]

LEGEND_ROWS = [
    ("Column", "What it means"),
    (
        "Eligible for You",
        "Filtered for your stated profile: first-year (class of 2030), male, not from a "
        "marginalized group, US-based, at Stanford.",
    ),
    (None, None),
    ("Code", "Meaning"),
    ("YES", "Verified open to you now. Apply."),
    ("LATER", "Right program, wrong year (or wrong identity-neutral stage). Diary it."),
    (
        "NO",
        "Ruled out by class year, identity criteria, geography, or the program no longer "
        "exists.",
    ),
    (
        "CHECK",
        "Program exists but eligibility is not published. Open the page yourself before "
        "assuming.",
    ),
    ("INVITE", "Entry is by invitation via campus recruiting, not open application."),
    ("VIA CLUB", "Entry runs through a university-sponsored team (Traders @ Stanford)."),
    ("LOW VALUE", "Open to you, but weak signal relative to the time it costs."),
    ("USE / USE WEEKLY", "A tracker or resource to monitor, not an application."),
    ("APPLIED - too early", "You have already applied and it was not yet the right cycle."),
    (None, None),
    (
        "Dates",
        "Dates are the PRIOR cycle's unless a 2026/2027 date is explicitly stated. Treat "
        "them as the window to start watching, not a deadline to trust.",
    ),
    (
        "Rolling firms",
        "Jane Street, NVIDIA, D. E. Shaw and Point72 review on a rolling basis and close "
        "when full. Week-one applications face the most open seats.",
    ),
    (None, None),
    (
        "Nearest hard deadlines",
        "Cornell CTC (opens early fall 2026) | SIG First Year Discovery (event Oct 2026) | "
        "UChicago (opens Nov 2026) | Google Student Researcher (Nov 27, 2026)",
    ),
    (
        "Corrections carried in",
        "Bridgewater Rising Fellows and Jane Street INSIGHT were recommended earlier in "
        "our conversation and are marked NO here - see their Notes.",
    ),
    (None, None),
    ("THIS FILE IS GENERATED", "Edit data/programs.csv, not this spreadsheet. Anything you type here is overwritten on the next run."),
    (
        "Tracking columns",
        "Source ID, Status, Last Checked, Last Changed, First Seen, Muted and Snapshot "
        "Hash are maintained by the tracker. Set Muted to true in the CSV to silence a "
        "noisy row.",
    ),
    (
        "Manual Watch sheet",
        "Opportunities this tool provably cannot alert you about - sites that block "
        "automation, competitions announced on Instagram or listservs before the website "
        "changes, and pages with nothing to diff. Each row says how and when to check by "
        "hand.",
    ),
    (
        "Priority sheet",
        "A ranked shortlist to keep an eye on yourself, in case this tracker breaks or "
        "goes quiet. Band A is high value with real selection risk; Band B is "
        "Stanford-internal, where your odds are genuinely best; Band C is open entry, "
        "where showing up is the only gate.",
    ),
]


def _write_sheet(worksheet, headers, rows, hyperlink_columns=()) -> None:
    for index, (_, title, width) in enumerate(headers, start=1):
        cell = worksheet.cell(1, index, title)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGN
        worksheet.column_dimensions[get_column_letter(index)].width = width
    worksheet.row_dimensions[1].height = 24

    for row_index, row in enumerate(rows, start=2):
        for column_index, (key, _, _) in enumerate(headers, start=1):
            value = row.get(key, "")
            cell = worksheet.cell(row_index, column_index, value)
            cell.font = BODY_FONT
            cell.alignment = BODY_ALIGN
            if key in hyperlink_columns and isinstance(value, str) and value.startswith("http"):
                cell.hyperlink = value
                cell.font = LINK_FONT
            if key == "eligible":
                style = ELIGIBLE_STYLES.get(str(value).strip())
                if style:
                    fill, colour = style
                    cell.fill = fill
                    cell.font = Font(name=ARIAL, size=10, bold=True, color=colour)

    last_column = get_column_letter(len(headers))
    last_row = len(rows) + 1
    worksheet.auto_filter.ref = f"A1:{last_column}{last_row}"
    worksheet.freeze_panes = "B2"


def _read(path: pathlib.Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build() -> pathlib.Path:
    programs = state.read_programs()
    if not programs:
        raise SystemExit("data/programs.csv is empty; run seed_programs.py first")

    workbook = Workbook()
    _write_sheet(
        workbook.active, PROGRAM_HEADERS, programs, hyperlink_columns={"website"}
    )
    workbook.active.title = "Programs"

    legend = workbook.create_sheet("Legend")
    legend.column_dimensions["A"].width = 24
    legend.column_dimensions["B"].width = 110
    for row_index, (left, right) in enumerate(LEGEND_ROWS, start=1):
        left_cell = legend.cell(row_index, 1, left)
        right_cell = legend.cell(row_index, 2, right)
        is_header = right == "What it means" or right == "Meaning" or (
            left is not None and left.isupper() and left.startswith("THIS FILE")
        )
        for cell in (left_cell, right_cell):
            cell.font = HEADER_FONT if is_header else BODY_FONT
            cell.alignment = HEADER_ALIGN if is_header else BODY_ALIGN
            if is_header:
                cell.fill = HEADER_FILL
        if left is not None and not is_header:
            left_cell.font = Font(name=ARIAL, size=10, bold=True)

    manual = _read(state.DATA / "manual.csv")
    if manual:
        _write_sheet(
            workbook.create_sheet("Manual Watch"),
            MANUAL_HEADERS,
            manual,
            hyperlink_columns={"url"},
        )

    priority = _read(state.DATA / "priority.csv")
    if priority:
        for row in priority:
            row["rank"] = int(row["rank"]) if str(row.get("rank", "")).isdigit() else row.get("rank", "")
        _write_sheet(
            workbook.create_sheet("Priority"),
            PRIORITY_HEADERS,
            priority,
            hyperlink_columns={"url"},
        )

    state.OUT_XLSX.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(state.OUT_XLSX)
    return state.OUT_XLSX


def main() -> int:
    path = build()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
