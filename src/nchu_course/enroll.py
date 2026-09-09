"""自動加選（純 HTTP，不開瀏覽器）。

對應手動操作：左側選單「輸入課號加選」→ 填課號 →「確定送出」→ 勾選 →「是，確定加選」。
走 requests 而不是瀏覽器，一次加選約 2 秒（開瀏覽器要 15 秒），搶課時差很多。

「只加不退」在這裡是結構性的保證，不是靠認頁面上的按鈕：

1. **只 POST 三個寫死的網址**（SSO 轉手、確認頁、送出），其餘一律拒送。退選相關頁
   （enro_del1_list、enro_rule1_list?v_type=D、enro_stop_list）不在名單內，
   程式裡沒有任何一條路徑走得到它們。
2. **送出前驗表單**：確認頁的 <form action> 必須正好是 enro_direct3_dml，不是就中止。
3. **勾選碼只從確認頁取**：v_tick 一定來自「那一列的課號欄正好等於目標課號」的
   那個 checkbox，不自己編，也不會送出別列的勾選碼。
4. **加選後再查一次選課清單**確認，結果不靠關鍵字猜。

任何一步跟預期不符就中止，什麼都不做——寧可沒加到，也不亂送。
"""
import datetime
import json
import os
import tempfile
import time
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

from nchu_course.config import (
    ENROLL_ALLOWED_POSTS,
    ENROLL_CHECK_MARKER,
    ENROLL_CHECK_URL,
    ENROLL_CODES,
    ENROLL_CODE_FIELD,
    ENROLL_COMMIT_URL,
    ENROLL_DEBUG_DIR,
    ENROLL_FILE,
    ENROLL_FORBIDDEN_URL_WORDS,
    ENROLL_FORM_MARKER,
    ENROLL_FORM_URL,
    ENROLL_KEEP_DEBUG,
    ENROLL_LIST_URL,
    ENROLL_MAX_ATTEMPTS,
    ENROLL_MAX_SLOTS,
    ENROLL_RETRY_AFTER,
    ENROLL_SSO_URL,
    ENROLL_SUBNAME,
    ENROLL_TICK_FIELD,
    ENROLL_URL,
    ENROLL_VERIFY,
    HTTP_TIMEOUT,
    LOGIN_URL,
    PORTAL_ACAD_URL,
    PORTAL_LOGIN_URL,
    ensure_data_dir,
    ensure_enroll_debug_dir,
)
from nchu_course.scraper import is_login_page


class EnrollAborted(RuntimeError):
    """流程中止：頁面不是預期的樣子。中止時什麼都沒送出。"""


# 結果頁的判讀。順序有意義：先認成功，再認各種失敗原因。
_RESULT_PATTERNS = (
    ("success", ("加選成功", "選課成功", "加選完成", "選課完成")),
    ("taken", ("已選過", "已加選過", "重複選課", "重覆選課", "已在選課清單", "曾修習此課程")),
    ("notfound", ("查無此", "查無資料", "查無課程", "課號錯誤", "無此課程")),
    ("conflict", ("衝堂", "時間衝突", "上課時間重複")),
    ("full", ("額滿", "名額已滿", "人數已滿", "無餘額", "餘額不足", "已達開課人數")),
    ("limit", ("學分上限", "超過學分", "已達上限", "學分數超過", "超修", "超過")),
    ("closed", ("不在選課時間", "非選課時間", "選課時間未到", "選課已結束", "尚未開放")),
    ("rejected", ("資格不符", "不符資格", "不符合資格", "非選課對象", "限本系", "無權限")),
)

# 結果頁那一列「選課結果」欄的失敗原因 -> 狀態。由上而下第一個命中者勝。
_REASON_STATUS = (
    ("full", ("額滿", "人數已滿", "名額", "已達開課人數", "無餘額")),
    ("conflict", ("衝堂", "時間衝突", "上課時間重複")),
    # 這裡只放明確指向「這門課你已經有了」的說法。刻意不用寬鬆的「已選」——
    # 「已選修超過4門通識課程」會被它搶先命中，那是門數上限不是重複選課。
    ("taken", ("該課程已選", "已選過", "已加選過", "重複選課", "重覆選課", "曾修習")),
    ("limit", ("超過", "上限", "學分", "門數", "超修")),
    ("closed", ("選課時間", "非選課", "尚未開放", "已結束")),
    ("rejected", ("資格", "限本系", "對象", "權限", "不符")),
)

# (emoji, 純文字)。推播與 log 一律用純文字——依專案慣例避開 Windows cp950；
# emoji 只出現在 Telegram 面板上（那裡不進 log）。
_STATUS_LABEL = {
    "success": ("✅", "加選成功"),
    "taken": ("ℹ️", "這門課已經在你的選課清單裡"),
    "notfound": ("⚠️", "查無此課號"),
    "conflict": ("⚠️", "衝堂，無法加選"),
    "full": ("⚠️", "名額已滿"),
    "limit": ("⚠️", "已達選課上限"),
    "closed": ("⚠️", "目前不在選課時間"),
    "rejected": ("⚠️", "不符選課資格"),
    "dry-run": ("🧪", "演練完成，沒有真的送出"),
    "unknown": ("❓", "已送出，但看不懂結果頁，請自己上系統確認"),
    "aborted": ("🛑", "已中止，沒有做任何動作"),
    "error": ("🛑", "執行失敗"),
}

# 這些結果再試也不會變，不必再跑
_FINAL_STATUSES = ("success", "taken", "conflict")


def _status_text(status):
    return _STATUS_LABEL.get(status, (None, status))[1]


def _short(text, limit=300):
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _page_text(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def _classify(text):
    """關鍵字判讀，給不是結果表格的頁面用（例如確認頁的錯誤訊息）。看不出來回傳 None。"""
    flat = " ".join((text or "").split())
    for status, keywords in _RESULT_PATTERNS:
        for keyword in keywords:
            index = flat.find(keyword)
            if index >= 0:
                start = max(0, index - 40)
                return status, flat[start:index + len(keyword) + 40].strip()
    return None


def _reason_status(reason):
    for status, keywords in _REASON_STATUS:
        if any(k in reason for k in keywords):
            return status
    return "rejected"


def parse_result(html, code):
    """從結果頁的表格讀出目標課號那一列的「選課結果」欄。

    這頁是結構化的（… | 授課教師 | 選課結果），直接讀那一格比在整頁找關鍵字準得多：
    「加選失敗，本學期已選修超過4門通識課程」用關鍵字會被誤判成「已選過」。
    """
    soup = BeautifulSoup(html or "", "html.parser")
    for row in soup.find_all("tr"):
        cells = [" ".join(td.get_text(" ", strip=True).split())
                 for td in row.find_all(["td", "th"])]
        if not any(cell == code for cell in cells):
            continue
        outcome = next((c for c in reversed(cells) if c), "")
        if not outcome:
            continue
        if "成功" in outcome:
            return "success", outcome
        if "失敗" in outcome:
            reason = outcome.split("，", 1)[-1].split(",", 1)[-1].strip() or outcome
            return _reason_status(reason), outcome
        return None
    return None


# ------------------------------------------------------------------ 結果物件

class EnrollResult:
    __slots__ = ("code", "status", "message", "course", "seconds", "at", "verified")

    def __init__(self, code, status, message="", course=None, verified=None):
        self.code = code
        self.status = status
        self.message = message
        self.course = course or {}      # 確認頁讀到的課程資訊
        self.verified = verified        # 事後查選課清單的結果：True/False/None
        self.seconds = 0.0
        self.at = datetime.datetime.now()

    @property
    def ok(self):
        return self.status == "success"

    @property
    def final(self):
        """True 代表再試也沒意義。"""
        return self.status in _FINAL_STATUSES

    @property
    def label(self):
        """帶 emoji 的說法，給 Telegram 面板用。"""
        emoji, text = _STATUS_LABEL.get(self.status, ("", self.status))
        return f"{emoji} {text}".strip()

    @property
    def plain_label(self):
        """純文字說法，給推播與 log 用。"""
        return _status_text(self.status)

    @property
    def title(self):
        name = self.course.get("name") or ""
        return f"{self.code} {name}".strip()

    def __repr__(self):
        return f"EnrollResult({self.code!r}, {self.status!r})"


# ------------------------------------------------------------------ 網址防線

def assert_post_allowed(url):
    """POST 之前的最後一道鎖：只有加選流程那三個網址放行。"""
    clean = url.split("?")[0]
    low = url.lower()
    for word in ENROLL_FORBIDDEN_URL_WORDS:
        if word in low:
            raise EnrollAborted(f"網址含「{word}」，拒絕送出：{url}")
    if not any(clean == allowed or clean.startswith(allowed)
               for allowed in ENROLL_ALLOWED_POSTS):
        raise EnrollAborted(f"網址不在加選白名單內，拒絕送出：{url}")
    return url


# ------------------------------------------------------------------ 頁面解析

def _forms(html, base):
    soup = BeautifulSoup(html or "", "html.parser")
    out = []
    for form in soup.find_all("form"):
        action = urljoin(base, (form.get("action") or "").strip())
        out.append((action, form))
    return out


def _auto_post_form(html, base):
    """解析「Redirect with POST」那張自動送出的 SSO 表單。"""
    for action, form in _forms(html, base):
        data = {i.get("name"): i.get("value", "")
                for i in form.find_all("input") if i.get("name")}
        if data:
            return action, data
    raise EnrollAborted("SSO 轉手頁沒有可送出的表單，教務系統可能改版了")


def _row_cells(element):
    """往上找到所在的 <tr>，回傳每一格的文字。"""
    row = element.find_parent("tr")
    if row is None:
        return []
    return [" ".join(td.get_text(" ", strip=True).split()) for td in row.find_all(["td", "th"])]


def _confirm_payload(html, code):
    """從確認頁挑出「目標課號那一列」的勾選碼，組成送出用的 payload。

    回傳 (payload, course_info)。沒有對應的可勾選列時回傳 (None, course_info)。
    """
    commit_form = None
    for action, form in _forms(html, ENROLL_CHECK_URL):
        if action.split("?")[0] == ENROLL_COMMIT_URL:
            commit_form = form
            break
    if commit_form is None:
        return None, {}

    hidden = {i.get("name"): i.get("value", "")
              for i in commit_form.find_all("input", attrs={"type": "hidden"})
              if i.get("name")}

    matched, seen = [], []
    for box in commit_form.find_all("input", attrs={"type": "checkbox"}):
        if box.get("name") != ENROLL_TICK_FIELD:
            continue
        cells = _row_cells(box)
        seen.append(cells)
        # 課號必須是「整格相符」，不接受包含關係
        if any(cell == code for cell in cells):
            matched.append((box.get("value") or "", cells))

    info = _course_info(matched[0][1] if matched else (seen[0] if seen else []), code)
    if len(matched) != 1:
        return None, info
    tick, _cells = matched[0]
    if not tick:
        raise EnrollAborted("勾選欄位沒有值，無法送出")
    payload = dict(hidden)
    payload[ENROLL_TICK_FIELD] = tick
    return payload, info


def _course_info(cells, code):
    """把確認頁那一列拆成看得懂的欄位（欄位順序來自實測）。"""
    if not cells:
        return {}
    info = {"row": " | ".join(cells)}
    try:
        index = cells.index(code)
    except ValueError:
        return info
    tail = cells[index + 1:]
    # 欄位順序來自確認頁表頭：
    # 選課號碼 | 課程名稱 | 必選修系所 | 全/半 | 學分 | 必/選 | 授課教師 | 開課人數 | 選課人數 | 候補人數
    keys = ("name", "dept", "half", "credits", "required", "teacher",
            "capacity", "enrolled", "waiting")
    for key, value in zip(keys, tail):
        info[key] = value
    return info


# ------------------------------------------------------------------ 加選主體

def _force_https(response, *args, **kwargs):
    """學校偶爾會回一個 http:// 的轉址，但校內只開 https（port 80 直接拒連），
    整趟就會卡在 Connection refused。這裡把 nchu 的轉址一律升級回 https。"""
    location = response.headers.get("Location")
    if location and location.startswith("http://") and "nchu.edu.tw" in location:
        response.headers["Location"] = "https://" + location[7:]
    return response


class Enroller:
    """一次加選的完整流程。session 沿用 NchuSession，cookie 與監控共用。"""

    def __init__(self, session=None, log=print, debug=False):
        if session is None:
            from nchu_course.session import NchuSession
            session = NchuSession(log=log)
        self.session = session
        self.log = log
        self.debug = debug
        hooks = self.session.s.hooks.setdefault("response", [])
        if _force_https not in hooks:
            hooks.append(_force_https)

    # ---------------------------------------------------------------- HTTP

    def _send(self, method, url, retry, **kwargs):
        """送出請求。retry=True 的（GET 與不會寫入的 POST）才會在斷線時重來一次。

        真正寫入的那一次（enro_direct3_dml）永遠不重送——寧可回報看不懂，
        也不要在不確定伺服器收到沒有的情況下再送一次。
        """
        attempts = 2 if retry else 1
        for attempt in range(1, attempts + 1):
            try:
                r = self.session.s.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
                r.raise_for_status()
                return r.text
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= attempts:
                    raise
                self.log(f"連線失敗（{type(exc).__name__}），1 秒後重試一次")
                time.sleep(1)

    def _get(self, url, referer=None, retry=True):
        headers = {"Referer": referer} if referer else {}
        return self._send("GET", url, retry, headers=headers)

    def _post(self, url, data, referer=None, retry=False):
        assert_post_allowed(url)                    # 白名單，最後一道鎖
        headers = {"Referer": referer} if referer else {}
        return self._send("POST", url, retry, data=data, headers=headers)

    # ------------------------------------------------------------ 登入三層

    @staticmethod
    def _portal_ok(html):
        """判斷是不是真的登入了校園入口。

        不能只看有沒有「登出」——Apereo CAS 自己的登入頁也有這兩個字，
        之前就這樣誤判成已登入，結果下一步才發現根本沒登入。改成先用
        is_login_page() 擋掉 CAS 表單（認 execution 欄位），再確認頁面上
        有入口登入後才會出現的東西。
        """
        if is_login_page(html):
            return False
        return any(marker in html for marker in ("上次登入時間", "我的最愛"))

    def _ensure_portal(self, force=False):
        """確保校園入口是登入狀態（沿用監控那套 L1/L2/L3）。"""
        if not force and self._portal_ok(self._get(PORTAL_LOGIN_URL)):
            self.log("入口 session 有效")
            return "L1"

        service = f"{LOGIN_URL}?service={quote(PORTAL_LOGIN_URL, safe='')}"
        if self.session.has_cookie("TGC"):
            self._get(service)
            if self._portal_ok(self._get(PORTAL_LOGIN_URL)):
                self.log("以 TGC 換票重新進入入口")
                self.session.save()
                return "L2"

        self.log("TGC 也失效，開瀏覽器重新登入")
        self.session.relogin()
        self._get(service)
        if not self._portal_ok(self._get(PORTAL_LOGIN_URL)):
            raise EnrollAborted("重新登入後仍進不了校園入口")
        self.session.save()
        return "L3"

    def _ensure_acad(self, retried=False):
        """走 acad SSO 轉手，讓教務資訊系統認得這個 session。

        入口那關過了不代表這關會過（session 可能剛好在兩個請求之間過期），
        被要求重新登入時就重建一次 session 再試，不要直接放棄。
        """
        html = self._get(f"{PORTAL_ACAD_URL}?p_subname={ENROLL_SUBNAME}",
                         referer=PORTAL_LOGIN_URL)
        action, data = ("", {})
        if not is_login_page(html):
            action, data = _auto_post_form(html, PORTAL_ACAD_URL)
        if is_login_page(html) or not action.split("?")[0].startswith(ENROLL_SSO_URL):
            if retried:
                raise EnrollAborted(f"重新登入後 SSO 仍指向非預期網址：{action or '登入頁'}")
            self.log("SSO 轉手被要求重新登入，重建 session 後再試一次")
            self._ensure_portal(force=True)
            return self._ensure_acad(retried=True)
        self._post(action, data, referer=PORTAL_ACAD_URL, retry=True)

    # ---------------------------------------------------------------- 流程

    def _open_form(self):
        html = self._get(ENROLL_FORM_URL, referer=ENROLL_URL)
        if ENROLL_FORM_MARKER not in html:
            raise EnrollAborted(
                f"加選表單頁不是預期的樣子（找不到「{ENROLL_FORM_MARKER}」）："
                + _short(_page_text(html), 120))
        for action, form in _forms(html, ENROLL_FORM_URL):
            if form.find("input", attrs={"name": ENROLL_CODE_FIELD}):
                if action.split("?")[0] != ENROLL_CHECK_URL:
                    raise EnrollAborted(f"加選表單指向非預期網址：{action}")
                return
        raise EnrollAborted(f"加選表單頁找不到 {ENROLL_CODE_FIELD} 欄位")

    def _submit_code(self, code):
        """填課號送出。只填第一格，其餘留空——一次只處理一門課。"""
        data = [(ENROLL_CODE_FIELD, code)]
        data += [(ENROLL_CODE_FIELD, "") for _ in range(ENROLL_MAX_SLOTS - 1)]
        html = self._post(ENROLL_CHECK_URL, data, referer=ENROLL_FORM_URL, retry=True)
        self._dump(code, "1-確認頁", html)
        return html

    def _commit(self, payload, code):
        html = self._post(ENROLL_COMMIT_URL, payload, referer=ENROLL_CHECK_URL)
        self._dump(code, "2-結果頁", html, always=True)
        return html

    def _verify(self, code):
        """加選後再查一次選課清單，結果不用猜。回傳 True/False/None。"""
        try:
            html = self._get(ENROLL_LIST_URL, referer=ENROLL_URL)
        except Exception as exc:
            self.log(f"選課清單查詢失敗，略過驗證: {exc}")
            return None
        soup = BeautifulSoup(html, "html.parser")
        cells = [" ".join(td.get_text(" ", strip=True).split())
                 for td in soup.find_all(["td", "th"])]
        if not cells:
            return None
        self._dump(code, "3-選課清單", html)
        return code in cells

    # ---------------------------------------------------------------- 對外

    def enroll(self, code, dry_run=False):
        code = str(code).strip()
        if not code:
            return EnrollResult(code, "aborted", "沒有給課號")

        self._ensure_portal()
        self._ensure_acad()
        self._open_form()

        html = self._submit_code(code)
        text = _page_text(html)
        if ENROLL_CHECK_MARKER not in html:
            status, message = _classify(text) or ("aborted", "送出課號後不是確認頁：" + _short(text, 200))
            return EnrollResult(code, status, message)

        payload, info = _confirm_payload(html, code)
        if payload is None:
            # 確認頁有出現，但沒有可勾選的目標課號（額滿、衝堂、查無…）
            status, message = _classify(text) or (
                "aborted", "確認頁沒有可勾選的目標課號：" + _short(text, 200))
            return EnrollResult(code, status, message, course=info)

        self.log(f"確認頁對到課號 {code}：{_short(info.get('row', ''), 120)}")
        if dry_run:
            return EnrollResult(
                code, "dry-run",
                f"已停在送出前。確認頁那一列：{_short(info.get('row', ''), 200)}", course=info)

        result_html = self._commit(payload, code)
        result_text = _page_text(result_html)
        guess = parse_result(result_html, code) or _classify(result_text)

        status, message = guess or ("unknown", _short(result_text, 300))

        # 結果頁講的是「這一次送出的結果」，最準，所以它說了算。
        # 事後查選課清單只用來抓兩種不一致，不能反過來蓋掉結果頁——
        # 課本來就在清單裡（重複加選）時，會把「加選失敗，該課程已選」誤報成成功。
        verified = self._verify(code) if ENROLL_VERIFY else None
        if status == "success" and verified is False:
            status = "unknown"
            message = "結果頁說加選成功，但選課清單查不到這門課，請自行確認：" + message
        elif status == "unknown" and verified is True:
            status = "taken"
            message = "看不懂結果頁，但選課清單裡已經有這門課：" + message
        return EnrollResult(code, status, message, course=info, verified=verified)

    # ---------------------------------------------------------------- 存檔

    def _dump(self, code, step, html, always=False):
        if not (self.debug or ENROLL_KEEP_DEBUG or always):
            return
        try:
            directory = ensure_enroll_debug_dir()
            path = directory / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{code}-{step}.html"
            path.write_text(html, encoding="utf-8")
            self.log(f"已存下頁面：{path.name}")
        except Exception as exc:
            self.log(f"存頁面失敗: {exc}")


def enroll_course(code, dry_run=False, debug=False, log=print, session=None):
    """加選一門課，回傳 EnrollResult。不會往外丟例外。"""
    import time

    code = str(code).strip()
    log(f"開始加選 {code}" + ("（演練，不會真的送出）" if dry_run else ""))
    started = time.monotonic()
    try:
        result = Enroller(session=session, log=log, debug=debug).enroll(code, dry_run=dry_run)
    except EnrollAborted as exc:
        result = EnrollResult(code, "aborted", str(exc))
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result = EnrollResult(code, "error", f"{type(exc).__name__}: {exc}")
    result.seconds = time.monotonic() - started
    log(f"加選 {code} 結束：{result.plain_label}"
        + (f" — {_short(result.message, 200)}" if result.message else "")
        + f"（{result.seconds:.1f}s）")
    return result


# ------------------------------------------------------------ 紀錄（避免重試）

def _load_records(log=print):
    path = str(ENROLL_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log(f"加選紀錄讀取失敗，視為空的: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _save_records(records):
    ensure_data_dir()
    path = str(ENROLL_FILE)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".enroll-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _parse_time(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def record_result(result, records=None, log=print):
    """把一次加選結果寫進 data/enroll.json，回傳更新後的紀錄。

    手動 `nchu enroll` 與 watch 的自動加選共用同一份紀錄，所以手動加到的課，
    watch 不會再送一次。演練不留紀錄——它沒有真的送出，不該佔用重試次數。
    """
    if result.status == "dry-run":
        return records if records is not None else _load_records(log)
    records = _load_records(log) if records is None else records
    previous = records.get(result.code) or {}
    # 成功就把次數歸零收工；失敗才累加（「連續失敗 N 次就不再試」）
    attempts = 0 if result.ok else int(previous.get("attempts") or 0) + 1
    records[result.code] = {
        "status": result.status,
        "attempts": attempts,
        "last": result.at.isoformat(timespec="seconds"),
        "message": _short(result.message, 300),
    }
    try:
        _save_records(records)
    except Exception as exc:
        log(f"加選紀錄寫入失敗: {exc}")
    return records


def clear_record(code=None, log=print):
    """清掉加選紀錄（達重試上限後想再試就用這個）。"""
    records = _load_records(log)
    if code is None:
        records = {}
    else:
        records.pop(str(code), None)
    _save_records(records)
    return records


# ------------------------------------------------------------ watch 的自動加選

class AutoEnroller:
    """watch 用：一發現名額就送出加選。

    只在 `nchu watch --autoadd`（或 settings.toml 的 `[enroll] auto = true`）
    時才會被建立；沒開就完全不存在，行為與加這個功能之前一模一樣。
    """

    def __init__(self, codes=None, notifier=None, session=None, log=print,
                 dry_run=False, debug=False):
        self.codes = [str(c) for c in (ENROLL_CODES if codes is None else codes)]
        self.notifier = notifier
        self.session = session
        self.log = log
        self.dry_run = dry_run
        self.debug = debug
        self.records = _load_records(log)

    @property
    def summary(self):
        mode = "演練" if self.dry_run else "開"
        return f"{mode}（{', '.join(self.codes) or '無課號'}）"

    def handle(self, courses):
        """對有名額的目標課程逐一嘗試加選，回傳這一輪的 EnrollResult 清單。"""
        results = []
        for course in courses:
            if course.code not in self.codes or not course.has_seat:
                continue
            skip = self._skip_reason(course.code)
            if skip:
                self.log(f"{course.code} 有名額，但略過自動加選：{skip}")
                continue
            results.append(self._attempt(course))
        return results

    # ------------------------------------------------------------------ 內部

    def _skip_reason(self, code):
        record = self.records.get(code)
        if not record:
            return None
        status = record.get("status")
        if status in _FINAL_STATUSES:
            return f"先前結果為「{_status_text(status)}」，不再重試"
        attempts = int(record.get("attempts") or 0)
        if attempts >= ENROLL_MAX_ATTEMPTS:
            return (f"已連續失敗 {attempts} 次，不再重試"
                    f"（想重來請刪掉 data/enroll.json）")
        last = _parse_time(record.get("last"))
        if last and ENROLL_RETRY_AFTER > 0:
            waited = (datetime.datetime.now() - last).total_seconds()
            if waited < ENROLL_RETRY_AFTER:
                return f"距上次嘗試才 {int(waited)}s，未滿 {ENROLL_RETRY_AFTER}s 冷卻"
        return None

    def _attempt(self, course):
        result = enroll_course(course.code, dry_run=self.dry_run, debug=self.debug,
                               log=self.log, session=self.session)
        self._record(result)
        self._notify(course, result)
        return result

    def _record(self, result):
        self.records = record_result(result, self.records, self.log)

    def _notify(self, course, result):
        if self.notifier is None:
            return
        text = f"[自動加選] {course.code} {course.name}\n{result.plain_label}"
        if result.message:
            text += f"\n{_short(result.message, 300)}"
        if result.ok:
            text += "\n（已在選課清單確認）" if result.verified else "\n請上選課系統再確認一次。"
        try:
            self.notifier.alert(text)
        except Exception as exc:
            self.log(f"加選結果通知送出失敗: {exc}")
