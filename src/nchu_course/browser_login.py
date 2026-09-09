"""L3：用 SeleniumBase UC mode 走完整密碼登入，取回跨網域 cookie。

只有在 CAS 的 TGC 也失效時才會被呼叫，所以這裡的十幾秒開銷
不影響每分鐘輪詢的平均成本。

`login_in_browser()` 與 `collect_cookies()` 拆出來給 enroll.py 共用：
加選也要開瀏覽器，cookie 失效時得在同一個視窗裡把密碼登入補完。
"""
import time
from urllib.parse import quote

from nchu_course.config import (
    COURSE_QUERY_URL,
    ELEMENT_TIMEOUT,
    LOGIN_URL,
    PASSWORD,
    RECONNECT_TIME,
    SELECTORS,
    USERNAME,
    WAIT_TIME,
)


class LoginFailed(RuntimeError):
    """密碼登入沒有成功走到課程查詢頁。"""


def _turnstile_token(sb):
    """讀 Turnstile 隱藏欄位的值。回傳 None 代表頁面上沒有這個欄位。"""
    return sb.execute_script(
        "var e=document.querySelector('[name=\"cf-turnstile-response\"]');"
        "return e ? e.value : null"
    )


def _ensure_turnstile(sb, log):
    """確認 Turnstile 已通過。UC mode 正常情況下會自動放行。"""
    for attempt in range(3):
        token = _turnstile_token(sb)
        if token is None:
            log("Turnstile: 頁面無此欄位，略過")
            return
        if token:
            log(f"Turnstile: 已通過 (token len={len(token)})")
            return
        log(f"Turnstile: 尚未通過，嘗試點擊 ({attempt + 1}/3)")
        try:
            sb.uc_gui_click_captcha()
        except Exception as exc:  # PyAutoGUI 在無桌面環境會失敗
            log(f"Turnstile: uc_gui_click_captcha 失敗 ({exc})")
        time.sleep(2)
    raise LoginFailed("Turnstile 驗證未通過")


def login_in_browser(sb, service_url=COURSE_QUERY_URL, log=print):
    """在一個已開好的瀏覽器裡走完 CAS 密碼登入，結束時停在 service_url 附近。

    呼叫端自己負責開關瀏覽器，也自己負責驗證有沒有真的抵達目的頁。
    """
    if not USERNAME or not PASSWORD:
        raise LoginFailed("缺少帳號或密碼，請確認 .env 的 NCHU_USERNAME / NCHU_PASSWORD")

    sb.uc_open_with_reconnect(f"{LOGIN_URL}?service={quote(service_url, safe='')}",
                              reconnect_time=RECONNECT_TIME)
    time.sleep(WAIT_TIME * 2)

    sb.type(SELECTORS["username_input"], USERNAME)
    sb.type(SELECTORS["password_input"], PASSWORD)
    _ensure_turnstile(sb, log)

    sb.wait_for_element_clickable(SELECTORS["submit_btn"], timeout=ELEMENT_TIMEOUT)
    sb.click(SELECTORS["submit_btn"])
    time.sleep(1)

    # 密碼登入後會多一張中繼確認頁（用 TGC 換票時不會出現）
    if sb.is_element_present(SELECTORS["continue_btn"]):
        sb.click(SELECTORS["continue_btn"])
        time.sleep(1)


def collect_cookies(sb):
    """回傳 (cookies, user_agent)，cookies 只含 nchu.edu.tw 相關網域。

    driver.get_cookies() 只回傳當前網域，拿不到另一邊；用 CDP 一次取全部。
    """
    raw = sb.driver.execute_cdp_cmd("Network.getAllCookies", {})["cookies"]
    user_agent = sb.execute_script("return navigator.userAgent")
    cookies = [
        {
            "name": c["name"],
            "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path") or "/",
        }
        for c in raw
        if "nchu.edu.tw" in (c.get("domain") or "")
    ]
    return cookies, user_agent


def browser_login(log=print):
    """開一個瀏覽器走完密碼登入並取回 cookie。

    cookies 是 [{name, value, domain, path}, ...]，只含 nchu.edu.tw 相關網域。
    """
    from seleniumbase import SB  # 延遲 import：只有 L3 需要，可省下 CLI 啟動時間

    with SB(uc=True) as sb:
        login_in_browser(sb, COURSE_QUERY_URL, log=log)

        sb.open(COURSE_QUERY_URL)
        time.sleep(1)
        if not sb.is_element_present(SELECTORS["subject_select"]):
            raise LoginFailed(f"登入後未抵達課程查詢頁，目前在 {sb.get_current_url()}")

        cookies, user_agent = collect_cookies(sb)

    log(f"瀏覽器登入成功，取得 {len(cookies)} 個 cookie: "
        f"{sorted(c['name'] for c in cookies)}")
    if not any(c["name"] == "TGC" for c in cookies):
        raise LoginFailed("登入流程完成但沒拿到 TGC，session 無法續命")
    return cookies, user_agent
