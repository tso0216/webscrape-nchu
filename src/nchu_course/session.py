"""三層 session 續命，讓每分鐘輪詢幾乎不用開瀏覽器。

L1  cportal 的 SESSION cookie 還有效 -> 直接 POST 查詢          (~0.9s)
L2  SESSION 過期但 CAS 的 TGC 還在   -> 用 TGC 換 ST 票再查詢    (~0.9s，無 Turnstile)
L3  TGC 也過期                       -> 開瀏覽器走密碼登入        (~13s)
"""
import json
import os
import tempfile
from urllib.parse import quote, urlparse

import requests

from nchu_course.browser_login import LoginFailed, browser_login
from nchu_course.config import (
    CAS_HOST,
    COOKIE_FILE,
    COURSE_QUERY_URL,
    COURSE_YEAR,
    DEFAULT_USER_AGENT,
    HTTP_TIMEOUT,
    LOGIN_URL,
    ensure_data_dir,
)
from nchu_course.scraper import build_query, is_login_page, parse_year


class RateLimited(RuntimeError):
    """伺服器回 429/503，應該退避而不是重新登入。"""


def load_cookie_file(path=COOKIE_FILE, log=print):
    """讀回 cookies.json，回傳 (cookies, user_agent)。

    讀不到就回 ([], 預設 UA)——呼叫端一律當成「沒有 session」處理。
    """
    path = str(path)
    if not os.path.exists(path):
        return [], DEFAULT_USER_AGENT
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log(f"cookies 檔讀取失敗，將重新登入: {exc}")
        return [], DEFAULT_USER_AGENT
    if not isinstance(data, dict):
        return [], DEFAULT_USER_AGENT
    return list(data.get("cookies") or []), data.get("user_agent") or DEFAULT_USER_AGENT


class NchuSession:
    def __init__(self, cookie_file=COOKIE_FILE, log=print):
        self.cookie_file = str(cookie_file)
        self.log = log
        self.user_agent = DEFAULT_USER_AGENT
        self.last_tier = None       # 上一次查詢走的層級："L1" / "L2" / "L3"
        self._year = COURSE_YEAR
        self._saved_signature = None
        self.s = None
        self._load()

    # ------------------------------------------------------------------ 對外

    def fetch_courses(self):
        """回傳課程查詢結果 HTML，必要時自動續命。"""
        html = self._try_query()
        if html is not None:
            self.last_tier = "L1"
            self.log("L1 命中：沿用既有 session")
            self._save_if_changed()
            return html

        if self.has_cookie("TGC") and self._renew_via_tgc():
            html = self._try_query()
            if html is not None:
                self.last_tier = "L2"
                self.log("L2 命中：以 TGC 換票續期")
                self._save_if_changed()
                return html

        self.last_tier = "L3"
        self.log("L3：TGC 失效，開瀏覽器重新登入")
        self.relogin()
        html = self._try_query()
        if html is None:
            raise LoginFailed("瀏覽器登入完成，但查詢仍被導回登入頁")
        self._save(force=True)
        return html

    def relogin(self):
        """強制走一次瀏覽器密碼登入，換掉現有 cookie。"""
        cookies, user_agent = browser_login(log=self.log)
        self.user_agent = user_agent
        self._new_session()
        for c in cookies:
            self._set_cookie(c)
        self._year = COURSE_YEAR
        self._save(force=True)

    def absorb_browser_cookies(self, cookies, user_agent=None):
        """把加選時在瀏覽器裡重新登入拿到的 cookie 併回來，省掉之後多跑一次 L3。

        整組換掉而不是逐一合併：新的一次登入會發新的 TGC / SESSION，
        舊的留著只會混淆判斷。
        """
        if not cookies:
            return
        if user_agent:
            self.user_agent = user_agent
        self._new_session()
        for c in cookies:
            self._set_cookie(c)
        self._save(force=True)
        self.log(f"已更新 {len(cookies)} 個 cookie（來自加選流程的登入）")

    def save(self):
        """cookie 有變就寫回檔案（加選流程換到新 session 時會用到）。"""
        self._save_if_changed()

    def clear(self):
        """刪掉已存的 cookie，下次查詢會重新登入。"""
        self._new_session()
        if os.path.exists(self.cookie_file):
            os.unlink(self.cookie_file)
        self._saved_signature = self._snapshot()

    def has_cookie(self, name):
        return any(c.name == name for c in self.s.cookies)

    def cookie_names(self):
        return sorted(c.name for c in self.s.cookies)

    # ------------------------------------------------------------------ 三層

    def _try_query(self):
        """送出查詢。session 失效回傳 None，成功回傳 HTML。"""
        if not self.has_cookie("SESSION"):
            return None
        try:
            if self._year is None:
                html = self._get(COURSE_QUERY_URL)
                if html is None:
                    return None
                self._year = parse_year(html)
                if self._year is None:
                    self.log("警告：查詢頁抓不到 p_year，改用頁面預設值送出")
            html = self._post(COURSE_QUERY_URL, build_query(self._year or ""))
            if html is None:
                return None
            year = parse_year(html)
            if year:
                self._year = year
            return html
        except requests.RequestException as exc:
            raise RuntimeError(f"查詢請求失敗: {exc}") from exc

    def _renew_via_tgc(self):
        """拿 TGC 去 CAS 換一張 service ticket，換回新的 cportal SESSION。"""
        url = f"{LOGIN_URL}?service={quote(COURSE_QUERY_URL, safe='')}"
        try:
            r = self.s.get(url, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            self.log(f"L2 換票失敗（網路錯誤）: {exc}")
            return False
        self._check_status(r)
        if urlparse(r.url).hostname == CAS_HOST or is_login_page(r.text):
            self.log("L2 換票失敗：TGC 已失效")
            return False
        return True

    # ------------------------------------------------------------------ HTTP

    def _get(self, url):
        return self._body_or_none(self.s.get(url, timeout=HTTP_TIMEOUT))

    def _post(self, url, data):
        return self._body_or_none(self.s.post(
            url, data=data, timeout=HTTP_TIMEOUT,
            headers={"Referer": COURSE_QUERY_URL}))

    def _body_or_none(self, r):
        self._check_status(r)
        if urlparse(r.url).hostname == CAS_HOST or is_login_page(r.text):
            return None
        return r.text

    @staticmethod
    def _check_status(r):
        if r.status_code in (429, 503):
            raise RateLimited(f"伺服器回 {r.status_code}，需要退避")
        r.raise_for_status()

    # ------------------------------------------------------------- cookie 管理

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": self.user_agent,
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def _set_cookie(self, c):
        # domain 保留原本的前置點（.nchu.edu.tw 才會跨子網域送出）
        self.s.cookies.set(c["name"], c["value"],
                           domain=c.get("domain") or "", path=c.get("path") or "/")

    def _snapshot(self):
        return sorted((c.name, c.value, c.domain, c.path) for c in self.s.cookies)

    def _load(self):
        cookies, self.user_agent = load_cookie_file(self.cookie_file, log=self.log)
        self._new_session()
        for c in cookies:
            self._set_cookie(c)
        self._saved_signature = self._snapshot()
        if cookies:
            self.log(f"載入 {len(cookies)} 個既有 cookie: {self.cookie_names()}")

    def _save_if_changed(self):
        if self._snapshot() != self._saved_signature:
            self._save(force=True)

    def _save(self, force=False):
        ensure_data_dir()
        payload = {
            "user_agent": self.user_agent,
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                for c in self.s.cookies
            ],
        }
        # 先寫暫存檔再 rename，避免程式中斷留下半截檔案
        directory = os.path.dirname(os.path.abspath(self.cookie_file)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".cookies-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cookie_file)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
        self._saved_signature = self._snapshot()
