"""設定載入：使用者設定讀 settings.toml，機密讀 .env，其餘為固定常數。"""
import os
import sys
import tomllib
from pathlib import Path

from dotenv import load_dotenv

# 專案根目錄 = src/nchu_course/config.py 往上三層
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
SETTINGS_FILE = ROOT / "settings.toml"

COOKIE_FILE = DATA_DIR / "cookies.json"
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "watch.log"
ENROLL_FILE = DATA_DIR / "enroll.json"
ENROLL_DEBUG_DIR = DATA_DIR / "enroll-debug"

load_dotenv(ROOT / ".env")

# ---- 機密（.env）----
# 一律用 NCHU_ 前綴：Windows 內建的 `USERNAME` 環境變數（作業系統帳號名）永遠存在，
# 而 load_dotenv 預設不覆寫既有環境變數，退路會讓漏填變成拿錯帳號去登入。
USERNAME = os.getenv("NCHU_USERNAME")
PASSWORD = os.getenv("NCHU_PASSWORD")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _load_settings():
    if not SETTINGS_FILE.exists():
        sys.exit(f"找不到設定檔 {SETTINGS_FILE}\n請從 settings.toml 範本複製一份。")
    try:
        with open(SETTINGS_FILE, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"settings.toml 格式錯誤：{exc}")


_S = _load_settings()
_courses = _S.get("courses", {})
_poll = _S.get("poll", {})
_retry = _S.get("retry", {})
_enroll = _S.get("enroll", {})

# ---- 課程（settings.toml [courses]）----
WATCH_CODES = [str(c) for c in _courses.get("watch", [])]
COURSE_CATEGORY_CODE = _courses.get("category", "EFGKM")
COURSE_YEAR = _courses.get("year") or None

# ---- 輪詢（settings.toml [poll]）----
POLL_INTERVAL = int(_poll.get("interval", 60))
POLL_JITTER = float(_poll.get("jitter", 5))
_quiet = _poll.get("quiet_hours") or []
QUIET_HOURS = tuple(_quiet) if len(_quiet) == 2 else None
QUIET_INTERVAL = int(_poll.get("quiet_interval", 600))

# ---- 重試（settings.toml [retry]）----
BACKOFF_SCHEDULE = [int(x) for x in _retry.get("backoff", [60, 120, 300, 900])]
FAIL_ALERT_THRESHOLD = int(_retry.get("alert_after", 3))

# ---- 自動加選（settings.toml [enroll]）----
# 預設關閉：這是唯一一個會「改動選課結果」的功能，必須明確打開才會動作。
ENROLL_AUTO = bool(_enroll.get("auto", False))
# 留空 = 沿用 [courses] watch 的全部課號
ENROLL_CODES = [str(c) for c in _enroll.get("codes", [])] or list(WATCH_CODES)
ENROLL_MAX_ATTEMPTS = int(_enroll.get("max_attempts", 3))
ENROLL_RETRY_AFTER = int(_enroll.get("retry_after", 60))
# 加選後是否再抓一次選課清單確認（多一個請求，但結果不用猜）
ENROLL_VERIFY = bool(_enroll.get("verify", True))
ENROLL_KEEP_DEBUG = bool(_enroll.get("keep_debug", False))

# ---- 站台常數（幾乎不會變，故不放進 settings.toml）----
LOGIN_URL = "https://ccidp.nchu.edu.tw/login"
COURSE_QUERY_URL = "https://cportal.nchu.edu.tw/cofsys/plsql/crseqry_gene_now"
CPORTAL_HOST = "cportal.nchu.edu.tw"

# ---- 加選流程的端點（實測自教務資訊系統）----
# 進入順序：入口 CAS 登入 -> acad SSO 轉手 -> 課號加選表單 -> 確認 -> 送出。
_COFSYS = "https://cportal.nchu.edu.tw/cofsys/plsql/"
PORTAL_LOGIN_URL = "https://cportal.nchu.edu.tw/cas_login/"
PORTAL_ACAD_URL = "https://cportal.nchu.edu.tw/cas_login/acad"
ENROLL_SUBNAME = "enro_direct1_list"            # 左側選單的「輸入課號加選」
ENROLL_SSO_URL = _COFSYS + "cof_ssologin"       # SSO 轉手（POST p_user）
ENROLL_FORM_URL = _COFSYS + ENROLL_SUBNAME      # 填課號的表單頁
ENROLL_CHECK_URL = _COFSYS + "enro_direct2_chk"  # 「確定送出」後的確認頁
ENROLL_COMMIT_URL = _COFSYS + "enro_direct3_dml"  # 「是，確定加選」真正寫入
ENROLL_LIST_URL = _COFSYS + "enro_stud_list"    # 選課清單（加選後用來驗證）
ENROLL_URL = _COFSYS + "stud_subframeset1?v_subname=" + ENROLL_SUBNAME

# 這支程式唯一允許 POST 的網址。清單以外一律拒送——退選頁（enro_del1_list、
# enro_rule1_list?v_type=D、enro_stop_list）不在名單內，程式沒有任何路徑打得到。
ENROLL_ALLOWED_POSTS = (ENROLL_SSO_URL, ENROLL_CHECK_URL, ENROLL_COMMIT_URL)
# 網址只要出現這些字樣就拒送，等於替上面的白名單再上一道鎖。
ENROLL_FORBIDDEN_URL_WORDS = ("_del", "delete", "drop", "_stop", "cancel", "remove",
                              "v_type=d")
ENROLL_MAX_SLOTS = 10       # 表單有 10 個 V_WANT 欄位
CAS_HOST = "ccidp.nchu.edu.tw"
HTTP_TIMEOUT = 30

TELEGRAM_API_BASE = "https://api.telegram.org"
# getUpdates 每次長輪詢的秒數。等待下一輪查詢的空檔就用它來監聽指令，
# 同時也是 Ctrl-C 之後最長的反應時間。
TELEGRAM_LONG_POLL = 10

# 結果表格的欄位索引（實測自 crseqry_gene_now 的 table#myTable01）
COL_CODE = 4      # 選課號碼
COL_NAME = 5      # 科目名稱
COL_CREDITS = 6   # 學分
COL_TIME = 7      # 上課時間
COL_TEACHER = 10  # 授課教師
COL_SEATS = 14    # 可選餘額

# 瀏覽器登入（L3）
WAIT_TIME = 0.1
RECONNECT_TIME = 4
ELEMENT_TIMEOUT = 15

SELECTORS = {
    "username_input": "input#username",
    "password_input": "input#password",
    "submit_btn": '[name="submitBtn"]',
    "continue_btn": '[name="continue"]',
    "turnstile_response": '[name="cf-turnstile-response"]',
    "subject_select": 'select[name="p_subject"]',
    "result_table": "table#myTable01 tbody",
}

# ---- 加選確認頁的必要標記（頁面改版時用來確認自己沒認錯頁）----
ENROLL_CHECK_MARKER = "課號加選-確認"
ENROLL_FORM_MARKER = "課號加選-填寫"
ENROLL_CODE_FIELD = "V_WANT"      # 課號輸入欄位名
ENROLL_TICK_FIELD = "v_tick"      # 確認頁的勾選欄位名

# 登入後會被實際的瀏覽器 UA 覆寫並存進 cookies.json
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def ensure_enroll_debug_dir():
    ENROLL_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    return ENROLL_DEBUG_DIR
