"""終端輸出：處理中日文全形字寬與 ANSI 色碼，讓表格能對齊。"""
import re
import sys
import unicodedata

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# 輸出被導向檔案或管線時關掉色碼，免得 log 裡塞滿 ANSI 逸出序列
# （舊版 cmd.exe 也不會解讀，會直接印成亂碼）。
_COLOR = sys.stdout.isatty()

GREEN = "\033[32m" if _COLOR else ""
RED = "\033[31m" if _COLOR else ""
DIM = "\033[2m" if _COLOR else ""
BOLD = "\033[1m" if _COLOR else ""
RESET = "\033[0m" if _COLOR else ""


def width(text):
    """字串在等寬終端機裡佔的欄數（全形字算 2，ANSI 色碼不計）。"""
    plain = _ANSI_RE.sub("", text)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in plain)


def pad(text, target, align="left"):
    fill = max(0, target - width(text))
    if align == "right":
        return " " * fill + text
    return text + " " * fill


def table(headers, rows, aligns=None):
    """把 headers/rows 排成對齊的表格字串。"""
    columns = len(headers)
    aligns = aligns or ["left"] * columns
    cells = [[str(c) for c in row] for row in rows]
    widths = [
        max([width(headers[i])] + [width(r[i]) for r in cells]) if cells
        else width(headers[i])
        for i in range(columns)
    ]

    out = ["  ".join(pad(headers[i], widths[i], aligns[i]) for i in range(columns)).rstrip()]
    out.append(DIM + "  ".join("─" * w for w in widths) + RESET)
    for row in cells:
        out.append("  ".join(pad(row[i], widths[i], aligns[i]) for i in range(columns)).rstrip())
    return "\n".join(out)


def course_rows(courses):
    """把 Course 清單轉成表格列，有名額的用綠色標示。"""
    rows = []
    for c in courses:
        mark = f"{GREEN}●{RESET}" if c.has_seat else f"{DIM}○{RESET}"
        seats = f"{GREEN}{c.seats}{RESET}" if c.has_seat else f"{DIM}{c.seats_text}{RESET}"
        rows.append([mark, c.code, c.name, c.teacher, c.credits, c.time, seats])
    return rows


COURSE_HEADERS = ["", "課號", "課程名稱", "授課教師", "學分", "時間", "餘額"]
COURSE_ALIGNS = ["left", "left", "left", "left", "right", "left", "right"]
