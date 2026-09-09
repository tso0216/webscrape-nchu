"""課程查詢的 payload 組裝與 HTML 解析（純資料處理，不碰網路與 session）。"""
import re

from bs4 import BeautifulSoup

from nchu_course.config import (
    COL_CODE,
    COL_CREDITS,
    COL_NAME,
    COL_SEATS,
    COL_TEACHER,
    COL_TIME,
    COURSE_CATEGORY_CODE,
    SELECTORS,
    WATCH_CODES,
)

# 查詢表單的完整欄位。p_check=1 是關鍵，換成 p_check=Y 會回傳 0 筆。
_BASE_PARAMS = {
    "p_group": "",
    "p_lang": "",
    "p_crsName": "",
    "p_teacher": "",
    "p_week": "",
    "p_mtg": "",
    "p_emi": "",
    "p_serial_no": "",
}

_YEAR_RE = re.compile(r'name="p_year"\s+value="(\d+)"')
_LOGIN_MARKERS = ('name="execution"', 'id="fm1"')


def build_query(year, subject=COURSE_CATEGORY_CODE):
    """組出 crseqry_gene_now 的 POST payload。"""
    return {"p_year": str(year), "p_subject": subject, "p_check": "1", **_BASE_PARAMS}


def parse_year(html):
    """從查詢頁抓出當前學年期（例如 '1151'），找不到回傳 None。"""
    m = _YEAR_RE.search(html)
    return m.group(1) if m else None


def is_login_page(html):
    """判斷回應是不是被踢回 CAS 登入頁。"""
    return any(marker in html for marker in _LOGIN_MARKERS)


class Course:
    """一門課。"""

    __slots__ = ("code", "name", "full_name", "teacher", "credits", "time", "seats")

    def __init__(self, code, name, full_name, teacher, credits, time, seats):
        self.code = code
        self.name = name            # 中文課名
        self.full_name = full_name  # 中文 + 英文完整課名
        self.teacher = teacher
        self.credits = credits
        self.time = time
        self.seats = seats  # int；欄位非數字（例如「夜校」）時為 None

    @property
    def has_seat(self):
        return isinstance(self.seats, int) and self.seats > 0

    @property
    def seats_text(self):
        return "-" if self.seats is None else str(self.seats)

    def __repr__(self):
        return f"Course({self.code!r}, {self.name!r}, seats={self.seats})"


def _cell(cells, index):
    """取一個儲存格的文字。

    用 get_text(" ") 而不是 get_text(strip=True)：後者會把中英文名稱之間的
    分隔吃掉，讓「設計思考 Design Thinking」黏成「設計思考Design Thinking」。
    """
    if index >= len(cells):
        return ""
    return " ".join(cells[index].get_text(" ", strip=True).split())


def iter_all_courses(html):
    """走訪查詢結果表格的每一列，產出 Course。

    找不到結果表格時丟 ValueError，讓上層能區分「沒名額」與「頁面壞掉」。
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(SELECTORS["result_table"])
    if table is None:
        raise ValueError(
            f"查詢結果頁找不到 {SELECTORS['result_table']}，可能 session 失效或頁面結構已變更")

    min_cols = max(COL_CODE, COL_NAME, COL_TEACHER, COL_TIME, COL_SEATS) + 1
    for row in table.select("tr"):
        cells = row.find_all("td")
        if len(cells) < min_cols:
            continue
        code = _cell(cells, COL_CODE)
        if not code:
            continue
        raw_seats = _cell(cells, COL_SEATS)
        try:
            seats = int(raw_seats)
        except ValueError:
            seats = None
        full_name = _cell(cells, COL_NAME)
        yield Course(
            code=code,
            name=full_name.split(" ")[0],
            full_name=full_name,
            teacher=_cell(cells, COL_TEACHER),
            credits=_cell(cells, COL_CREDITS),
            time=_cell(cells, COL_TIME),
            seats=seats,
        )


def parse_courses(html, wanted=None):
    """取出 wanted 名單內的課程，依 wanted 的順序回傳。"""
    wanted = WATCH_CODES if wanted is None else list(wanted)
    lookup = set(wanted)
    found = {}
    for course in iter_all_courses(html):
        if course.code in lookup and course.code not in found:
            found[course.code] = course
    return [found[c] for c in wanted if c in found]
