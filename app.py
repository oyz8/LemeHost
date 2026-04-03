#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import ssl
import time
import json
import threading
import random
import ddddocr
import requests
import websocket
import gradio as gr
from datetime import datetime, timezone, timedelta
from collections import deque
from fastapi import FastAPI
from fastapi.responses import JSONResponse

# ============================================================
# 环境变量
# ============================================================
LEME = os.environ.get("LEME", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
TG_API = os.environ.get("TG_API", "https://api.telegram.org")
PROJECT_URL = os.environ.get("PROJECT_URL", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
RENEW_THRESHOLD = int(os.environ.get("RENEW_THRESHOLD", "900"))

# ============================================================
# 常量
# ============================================================
BASE_URL = "https://lemehost.com"
LOGIN_URL = f"{BASE_URL}/site/login"
SERVER_INDEX_URL = f"{BASE_URL}/server/index"
MAX_LOGIN_RETRY = 30
SIGNATURE = "Leme Host Auto Renewal"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# ============================================================
# 共享状态
# ============================================================
worker_status = {
    "status": "waiting", "accounts": 0, "servers": 0,
    "checks": 0, "renewals": 0, "skipped": 0, "failures": 0, "starts": 0,
    "last_check": None, "next_check": None, "start_time": None,
    "keepalive": None, "server_info": [],
}
log_queue = deque(maxlen=200)


def add_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    log_queue.append(line)


def mask(text: str) -> str:
    if not text:
        return "***"
    if "@" in text:
        local, domain = text.split("@", 1)
        return f"{local[:3]}***@{domain}"
    return "***"


# ============================================================
# 保活
# ============================================================
def add_keepalive_task():
    if not PROJECT_URL:
        worker_status["keepalive"] = "skipped"
        return
    try:
        r = requests.post("https://trans.ct8.pl/add-url", json={"url": PROJECT_URL}, timeout=30)
        worker_status["keepalive"] = "success" if r.status_code == 200 else "failed"
        add_log(f"[KEEP] {'✅ 成功' if r.status_code == 200 else '❌ 失败'}")
    except Exception as e:
        worker_status["keepalive"] = "failed"
        add_log(f"[KEEP] ❌ {e}")


# ============================================================
# 解析账号 / TG / 工具
# ============================================================
def parse_accounts(raw: str) -> list:
    accounts = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line or "-----" not in line:
            continue
        parts = line.split("-----", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            accounts.append({"email": parts[0].strip(), "password": parts[1].strip()})
    return accounts


def send_telegram(text: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(f"{TG_API}/bot{TG_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TG_CHAT_ID, "text": text}, timeout=30)
        add_log("[TG] ✅ 通知已发送")
    except Exception as e:
        add_log(f"[TG] ❌ {e}")


def ts_to_cn(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone(timedelta(hours=8)))
    return dt.strftime("%Y年%m月%d日 %H时%M分")


def ts_remaining(ts_ms: int) -> int:
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return max(0, (ts_ms - now_ms) // 1000)


def fmt_seconds(s: int) -> str:
    if s <= 0:
        return "已过期"
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分{s % 60}秒"
    return f"{s // 3600}时{(s % 3600) // 60}分"


def fmt_runtime(start: float) -> str:
    if not start:
        return "--"
    m = (time.time() - start) / 60
    return f"{m:.0f}分" if m < 60 else f"{m / 60:.1f}时"


# ============================================================
# 续期核心类（每个账号独立实例）
# ============================================================
class LemeHostRenewer:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        self.ocr = ddddocr.DdddOcr(show_ad=False)
        self.logged_in = False
        self._started_servers = set()

    def _ex(self, pattern: str, html: str) -> str:
        m = re.search(pattern, html)
        return m.group(1) if m else ""

    def _solve_captcha(self, cap_url, min_len=6, max_len=6, max_try=15):
        """通用验证码识别"""
        for ct in range(1, max_try + 1):
            try:
                img_resp = self.session.get(cap_url, timeout=15)
                result = self.ocr.classification(img_resp.content)
                if result and re.match(rf'^[a-zA-Z]{{{min_len},{max_len}}}$', result):
                    add_log(f"    [OCR] #{ct}: '{result}' ✅")
                    return result
                else:
                    add_log(f"    [OCR] #{ct}: '{result}' (非{min_len}-{max_len}位)")
            except Exception as e:
                add_log(f"    [OCR] #{ct}: 异常 {e}")
            try:
                ref = self.session.get(f"{BASE_URL}/site/captcha?refresh=1", timeout=10)
                u = ref.json().get("url", "")
                if u:
                    cap_url = u if u.startswith("http") else BASE_URL + u
            except Exception:
                pass
            time.sleep(random.uniform(0.3, 0.6))
        return ""

    # ── 登录 ──
    def login(self) -> bool:
        total_captcha = [0]
        for attempt in range(1, MAX_LOGIN_RETRY + 1):
            add_log(f"[LOGIN] 尝试 {attempt}/{MAX_LOGIN_RETRY}: {mask(self.email)}")
            try:
                try:
                    self.session.get(BASE_URL, timeout=15)
                    time.sleep(random.uniform(1, 2))
                except Exception:
                    pass

                resp = self.session.get(LOGIN_URL, timeout=30)
                html = resp.text

                if "loginform-email" not in html:
                    if "challenge" in html.lower() or "cloudflare" in html.lower() or len(html) < 1000:
                        wait = 10 + attempt * 3
                        add_log(f"[LOGIN] ⚠️ CF 拦截，等待 {wait}s...")
                        time.sleep(wait)
                    else:
                        add_log("[LOGIN] ❌ 登录页无表单")
                        time.sleep(3)
                    continue

                csrf = self._ex(r'name="_csrf-frontend"\s+value="([^"]+)"', html)
                if not csrf:
                    csrf = self._ex(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
                if not csrf:
                    add_log("[LOGIN] ❌ CSRF 失败")
                    continue

                key = self._ex(r'id="loginform-key"[^>]*value="([^"]*)"', html) or ""
                cap_url = self._ex(r'id="loginform-verifycode-image"\s+src="([^"]+)"', html)
                if not cap_url:
                    continue
                if cap_url.startswith("/"):
                    cap_url = BASE_URL + cap_url

                # 识别验证码（严格6位字母）
                captcha = ""
                for ct in range(1, 6):
                    total_captcha[0] += 1
                    try:
                        img_resp = self.session.get(cap_url, timeout=15)
                        result = self.ocr.classification(img_resp.content)
                        if result and re.match(r'^[a-zA-Z]{6,7}$', result):
                            captcha = result
                            add_log(f"  [OCR] #{total_captcha[0]}: '{result}' ✅")
                            break
                        else:
                            add_log(f"  [OCR] #{total_captcha[0]}: '{result}' (非6-7位)")
                    except Exception as e:
                        add_log(f"  [OCR] #{total_captcha[0]}: 异常 {e}")
                    try:
                        ref = self.session.get(f"{BASE_URL}/site/captcha?refresh=1", timeout=10)
                        u = ref.json().get("url", "")
                        if u:
                            cap_url = u if u.startswith("http") else BASE_URL + u
                    except Exception:
                        pass
                    time.sleep(random.uniform(0.3, 0.6))

                if not captcha:
                    add_log("[LOGIN] ⏭️ 本轮无6位结果")
                    continue

                resp = self.session.post(LOGIN_URL, data={
                    "_csrf-frontend": csrf,
                    "LoginForm[email]": self.email,
                    "LoginForm[password]": self.password,
                    "LoginForm[verifyCode]": captcha,
                    "LoginForm[key]": key,
                    "LoginForm[key2]": "",
                    "LoginForm[rememberMe]": "1",
                    "login-button": "",
                }, timeout=30, allow_redirects=True, headers={
                    "Referer": LOGIN_URL, "Origin": BASE_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                })

                if "Logout" in resp.text:
                    add_log(f"[LOGIN] ✅ 成功: {mask(self.email)} (第{attempt}次, 共{total_captcha[0]}次OCR)")
                    self.logged_in = True
                    return True
                if "verification code is incorrect" in resp.text.lower() or "Invalid CAPTCHA" in resp.text:
                    add_log(f"[LOGIN] ❌ 验证码错误 '{captcha}'")
                    time.sleep(random.uniform(0.5, 1.5))
                    continue
                if "Incorrect email or password" in resp.text:
                    add_log(f"[LOGIN] ❌ 密码错误: {mask(self.email)}")
                    return False
            except Exception as e:
                add_log(f"[LOGIN] ❌ 异常: {e}")
                time.sleep(random.uniform(3, 6))

        add_log(f"[LOGIN] ❌ 失败: {mask(self.email)}")
        return False

    def ensure_login(self) -> bool:
        try:
            resp = self.session.get(SERVER_INDEX_URL, timeout=30)
            if "Logout" in resp.text:
                return True
        except Exception:
            pass
        add_log(f"[SESSION] 🔄 重新登录: {mask(self.email)}")
        self.logged_in = False
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        })
        return self.login()

    def get_servers(self) -> list:
        add_log("[SERVERS] 获取列表...")
        try:
            resp = self.session.get(SERVER_INDEX_URL, timeout=30)
            html = resp.text
        except Exception as e:
            add_log(f"[SERVERS] ❌ {e}")
            return []
        servers, seen = [], set()
        for m in re.finditer(r"/server/view\?id=(\d+)", html):
            sid = m.group(1)
            if sid in seen:
                continue
            seen.add(sid)
            nm = re.search(rf'data-key="{sid}".*?<h3[^>]*>(.*?)</h3>', html, re.DOTALL)
            name = re.sub(r"<[^>]+>", "", nm.group(1)).strip() if nm else "Unknown"
            servers.append((sid, name))
            add_log(f"[SERVERS] 🖥️ {sid} - {name}")
        add_log(f"[SERVERS] 共 {len(servers)} 台")
        return servers

    # ── WS 检查状态 + 开机 ──
    def _check_and_start_via_ws(self, server_id: str) -> str:
        """返回: 'started' / 'already_running' / 'failed'"""
        view_url = f"{BASE_URL}/server/view?id={server_id}"
        try:
            resp = self.session.get(view_url, timeout=30)
            html = resp.text

            ws_url_raw = self._ex(r'data-ws="([^"]+)"', html)
            if not ws_url_raw:
                add_log(f"  [WS] ❌ 未找到 data-ws")
                return "failed"

            ws_url = re.sub(r':\d+', '', ws_url_raw)
            page_token = self._ex(r'data-token="([^"]+)"', html)
            token_url = self._ex(r'data-token_url="([^"]+)"', html)
            csrf = self._ex(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)

            # 获取新鲜 token
            ws_token = page_token
            if token_url:
                token_url = token_url.replace("&amp;", "&")
                if "force=true" not in token_url:
                    token_url = token_url.replace("force=", "force=true")
                try:
                    tr = self.session.get(token_url, timeout=15, headers={
                        "Accept": "*/*", "X-Requested-With": "XMLHttpRequest",
                        "X-CSRF-TOKEN": csrf or "", "Referer": view_url,
                    })
                    if tr.status_code == 200:
                        td = tr.json()
                        ws_token = td.get("websocket_token", page_token)
                        ret_ws = td.get("websocket_url", "")
                        if ret_ws:
                            ws_url = re.sub(r':\d+', '', ret_ws)
                except Exception:
                    pass

            if not ws_token:
                add_log(f"  [WS] ❌ 无 token")
                return "failed"

            add_log(f"  [WS] 连接 {server_id}...")

            ws = websocket.WebSocket()
            ws.connect(
                ws_url,
                origin="https://lemehost.com",
                host=re.search(r'wss://([^/]+)', ws_url).group(1),
                header=[f"User-Agent: {USER_AGENT}", "Cache-Control: no-cache"],
                sslopt={"cert_reqs": ssl.CERT_NONE},
                timeout=15,
            )

            ws.send(json.dumps({"event": "auth", "args": [ws_token]}))

            start_time = time.time()
            authed = False
            sent_start = False

            while time.time() - start_time < 15:
                try:
                    ws.settimeout(3)
                    msg = ws.recv()
                    if not msg:
                        break

                    data = json.loads(msg)
                    event = data.get("event", "")
                    args = data.get("args", [])

                    if event == "auth success":
                        authed = True

                    elif event == "status":
                        status = args[0] if args else ""
                        add_log(f"  [WS] {server_id} 状态: {status}")

                        if status == "offline":
                            add_log(f"  [WS] ✅ 确认 offline，开机...")
                            ws.send(json.dumps({"event": "set state", "args": ["start"]}))
                            sent_start = True
                            time.sleep(2)
                            try:
                                ws.close()
                            except Exception:
                                pass
                            self._started_servers.add(server_id)
                            worker_status["starts"] += 1
                            return "started"

                        elif status == "stopping":
                            add_log(f"  [WS] ⏳ 正在停止，等待...")
                            # 继续监听等 offline

                        elif status in ["starting", "running"]:
                            add_log(f"  [WS] ✅ 已在线 ({status})")
                            try:
                                ws.close()
                            except Exception:
                                pass
                            self._started_servers.add(server_id)
                            return "already_running"

                    elif event == "stats":
                        try:
                            stats = json.loads(args[0]) if args else {}
                            state = stats.get("state", "")
                            if state == "offline" and authed and not sent_start:
                                add_log(f"  [WS] stats offline，开机...")
                                ws.send(json.dumps({"event": "set state", "args": ["start"]}))
                                sent_start = True
                                time.sleep(2)
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                                self._started_servers.add(server_id)
                                worker_status["starts"] += 1
                                return "started"
                            elif state in ["starting", "running"]:
                                try:
                                    ws.close()
                                except Exception:
                                    pass
                                self._started_servers.add(server_id)
                                return "already_running"
                        except Exception:
                            pass

                    elif event == "token expired":
                        break

                except websocket.WebSocketTimeoutException:
                    continue
                except Exception:
                    break

            try:
                ws.close()
            except Exception:
                pass
            return "failed"

        except Exception as e:
            add_log(f"  [WS] ❌ {e}")
            return "failed"

    # ── 检查 + 开机 + 续期 ──
    def check_and_renew(self, server_id, server_name=""):
        result = {
            "success": False, "server_id": server_id, "server_name": server_name,
            "old_expiry": "", "new_expiry": "", "message": "", "remaining": "",
            "email": self.email, "skipped": False, "remain_seconds": -1, "started": False,
        }
        url = f"{BASE_URL}/server/{server_id}/free-plan"
        try:
            resp = self.session.get(url, timeout=30)
            html = resp.text
            auto_ts = 0
            m = re.search(r'id="countdown"\s+data-timestamp="(\d+)"', html)
            if m: auto_ts = int(m.group(1))
            if not auto_ts:
                m = re.search(r'data-timestamp="(\d+)"[^>]*id="countdown"', html)
                if m: auto_ts = int(m.group(1))
            remain = ts_remaining(auto_ts) if auto_ts else -1
            del_ts = 0
            m = re.search(r'countdown-free-plan-delete[^>]*data-timestamp="(\d+)"', html)
            if m: del_ts = int(m.group(1))
            # ── 是否需要开机 ──
            need_check = False
            if remain == 0:
                need_check = True
                add_log(f"  [CHECK] {server_id} ⚠️ 倒计时过期")
                self._started_servers.discard(server_id)
            if "was recently stopped" in html or "reason of inactivity" in html:
                need_check = True
                add_log(f"  [CHECK] {server_id} ⚠️ 停机提示")
            if server_id in self._started_servers and remain > 0:
                need_check = False
            if need_check:
                ws_result = self._check_and_start_via_ws(server_id)
                if ws_result == "started":
                    result["started"] = True
                    add_log("  [CHECK] ⏳ 等待开机...")
                    time.sleep(10)
                    resp = self.session.get(url, timeout=30)
                    html = resp.text
                    auto_ts = 0
                    m = re.search(r'id="countdown"\s+data-timestamp="(\d+)"', html)
                    if m: auto_ts = int(m.group(1))
                    remain = ts_remaining(auto_ts) if auto_ts else -1
                    del_ts = 0
                    m = re.search(r'countdown-free-plan-delete[^>]*data-timestamp="(\d+)"', html)
                    if m: del_ts = int(m.group(1))
                elif ws_result == "already_running":
                    pass
                else:
                    add_log(f"  [CHECK] {server_id} ⚠️ WS 检查失败")
            if remain > 0:
                self._started_servers.discard(server_id)
            result["remain_seconds"] = remain
            if remain >= 0:
                result["remaining"] = fmt_seconds(remain)
                add_log(f"  [CHECK] {server_id} 剩余: {fmt_seconds(remain)} ({remain}s)")
                if remain > RENEW_THRESHOLD:
                    result["skipped"] = True
                    result["message"] = f"剩余 {fmt_seconds(remain)}，无需续期"
                    if del_ts:
                        result["old_expiry"] = result["new_expiry"] = ts_to_cn(del_ts)
                    return result
            else:
                add_log(f"  [CHECK] {server_id} 未获取到倒计时")
            if del_ts:
                result["old_expiry"] = ts_to_cn(del_ts)
            csrf = self._ex(r'name="_csrf-frontend"\s+value="([^"]+)"', html)
            if not csrf:
                csrf = self._ex(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html)
            if not csrf:
                result["message"] = "CSRF 获取失败"
                return result
            # ── 检测续期页是否需要验证码 ──
            has_captcha = "extendfreeplanform-captcha-image" in html
            captcha_value = ""
            if has_captcha:
                add_log(f"  [RENEW] ⚠️ 续期需要验证码!")
                cap_url = self._ex(r'id="extendfreeplanform-captcha-image"\s+src="([^"]+)"', html)
                if cap_url and cap_url.startswith("/"):
                    cap_url = BASE_URL + cap_url
                if cap_url:
                    captcha_value = self._solve_captcha(cap_url, min_len=6, max_len=7, max_try=15)
                if not captcha_value:
                    add_log("  [RENEW] ❌ 续期验证码识别失败")
                    result["message"] = "续期验证码识别失败"
                    return result
            add_log(f"  [RENEW] 🔄 续期: {server_id}" + (f" (captcha={captcha_value})" if captcha_value else ""))
            time.sleep(random.uniform(0.5, 1.5))
            # ── 提交续期（最多重试30轮验证码） ──
            for renew_try in range(30):
                self.session.post(url, data={
                    "_csrf-frontend": csrf,
                    "ExtendFreePlanForm[captcha]": captcha_value,
                }, timeout=30, headers={
                    "Referer": url, "Origin": BASE_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-PJAX": "true", "X-PJAX-Container": "#p0",
                })
                time.sleep(random.uniform(1, 2))
                resp3 = self.session.get(url, timeout=30)
                html3 = resp3.text
                # 检查验证码是否错误
                if has_captcha and ("verification code is incorrect" in html3.lower() or "Captcha cannot be blank" in html3):
                    add_log(f"  [RENEW] ❌ 续期验证码错误 (第{renew_try + 1}次)")
                    csrf = self._ex(r'name="_csrf-frontend"\s+value="([^"]+)"', html3)
                    if not csrf:
                        csrf = self._ex(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', html3)
                    cap_url = self._ex(r'id="extendfreeplanform-captcha-image"\s+src="([^"]+)"', html3)
                    if cap_url and cap_url.startswith("/"):
                        cap_url = BASE_URL + cap_url
                    if cap_url and csrf:
                        captcha_value = self._solve_captcha(cap_url, min_len=6, max_len=7, max_try=15)
                        if captcha_value:
                            continue
                    break
                else:
                    break
            # ── 验证结果 ──
            new_del = 0
            m = re.search(r'countdown-free-plan-delete[^>]*data-timestamp="(\d+)"', html3)
            if m: new_del = int(m.group(1))
            new_auto = 0
            m = re.search(r'id="countdown"\s+data-timestamp="(\d+)"', html3)
            if m: new_auto = int(m.group(1))
            if new_del:
                result["new_expiry"] = ts_to_cn(new_del)
            if new_auto:
                nr = ts_remaining(new_auto)
                result["remaining"] = fmt_seconds(nr)
                result["remain_seconds"] = nr
            if del_ts > 0 and new_del > del_ts:
                result["success"] = True
                result["message"] = "续期成功"
                add_log(f"  [RENEW] ✅ 成功! {result['old_expiry']} -> {result['new_expiry']}")
            elif new_del > 0 and del_ts == 0:
                result["success"] = True
                result["message"] = "续期成功"
            elif del_ts > 0 and new_del == del_ts:
                now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
                if new_del > now_ms:
                    result["success"] = True
                    result["message"] = "续期成功（有效期内）"
                else:
                    result["message"] = "到期时间未变化"
            else:
                result["message"] = "续期结果未知"
        except Exception as e:
            result["message"] = f"异常: {e}"
            add_log(f"  [RENEW] ❌ {e}")
        return result


# ============================================================
# Worker
# ============================================================
class RenewalWorker:
    def __init__(self):
        self.accounts = parse_accounts(LEME)
        self.renewers = []
        self.server_map = {}

    def run(self):
        worker_status["start_time"] = time.time()
        worker_status["accounts"] = len(self.accounts)
        add_log("🎮 Leme Host Auto Renewal 启动")
        add_log(f"📋 账号: {len(self.accounts)} | 间隔: {CHECK_INTERVAL}s | 阈值: {RENEW_THRESHOLD}s")
        add_keepalive_task()

        if not self.accounts:
            worker_status["status"] = "no_account"
            return

        for acc in self.accounts:
            r = LemeHostRenewer(acc["email"], acc["password"])
            if r.login():
                servers = r.get_servers()
                self.server_map[acc["email"]] = servers
                worker_status["servers"] += len(servers)
                self.renewers.append(r)
            else:
                send_telegram(f"❌ 登录失败\n\n账号：{acc['email']}\n\n{SIGNATURE}")

        if not self.renewers:
            worker_status["status"] = "login_failed"
            return

        worker_status["status"] = "running"
        send_telegram(
            f"🎮 Leme Host Renewal 已启动\n\n"
            f"账号: {len(self.renewers)} | 服务器: {worker_status['servers']}\n"
            f"间隔: {CHECK_INTERVAL}s | 阈值: {RENEW_THRESHOLD}s\n\n{SIGNATURE}"
        )

        while worker_status["status"] == "running":
            self._check_all()
            worker_status["checks"] += 1
            worker_status["last_check"] = datetime.now().strftime("%H:%M:%S")
            worker_status["next_check"] = (
                datetime.now() + timedelta(seconds=CHECK_INTERVAL)
            ).strftime("%H:%M:%S")
            add_log(
                f"[LOOP] #{worker_status['checks']} | "
                f"续={worker_status['renewals']} 跳={worker_status['skipped']} "
                f"败={worker_status['failures']} 开={worker_status['starts']} | "
                f"下次: {worker_status['next_check']}"
            )
            time.sleep(CHECK_INTERVAL)

    def _check_all(self):
        for renewer in self.renewers:
            email = renewer.email
            if not renewer.ensure_login():
                worker_status["failures"] += 1
                continue

            if worker_status["checks"] > 0 and worker_status["checks"] % 10 == 0:
                servers = renewer.get_servers()
                if servers:
                    self.server_map[email] = servers

            for sid, sname in self.server_map.get(email, []):
                r = renewer.check_and_renew(sid, sname)
                self._update_info(sid, sname, r)

                if r.get("skipped"):
                    worker_status["skipped"] += 1
                    continue

                if r["success"]:
                    worker_status["renewals"] += 1
                else:
                    worker_status["failures"] += 1

                emoji = "✅ 续期成功" if r["success"] else "❌ 续期失败"
                exp = ""
                if r["old_expiry"] and r["new_expiry"]:
                    exp = f"到期: {r['old_expiry']} -> {r['new_expiry']}"
                elif r["new_expiry"]:
                    exp = f"到期: {r['new_expiry']}"

                lines = [emoji, "", f"账号：{email}", f"服务器: {sid}"]
                if r.get("started"):
                    lines.append("🟢 已自动开机")
                if exp:
                    lines.append(exp)
                if not r["success"] and r["message"]:
                    lines.append(f"原因: {r['message']}")
                lines += ["", SIGNATURE]
                send_telegram("\n".join(lines))
                time.sleep(random.uniform(1, 2))

    def _update_info(self, sid, sname, r):
        info = {
            "id": sid, "name": sname,
            "remaining": r.get("remaining", "--"),
            "remain_seconds": r.get("remain_seconds", -1),
            "expiry": r.get("new_expiry") or r.get("old_expiry") or "--",
            "last_action": "开机+续期" if r.get("started") else ("续期" if not r.get("skipped") else "跳过"),
            "success": r.get("success", False),
            "started": r.get("started", False),
            "time": datetime.now().strftime("%H:%M:%S"),
            "email": mask(r.get("email", "")),
        }
        for i, s in enumerate(worker_status["server_info"]):
            if s["id"] == sid:
                worker_status["server_info"][i] = info
                return
        worker_status["server_info"].append(info)
# ============================================================
# 启动
# ============================================================
def auto_start():
    if not LEME:
        add_log("❌ 未设置 LEME")
        worker_status["status"] = "no_account"
        add_keepalive_task()
        return
    RenewalWorker().run()
threading.Thread(target=auto_start, daemon=True).start()
# ============================================================
# FastAPI
# ============================================================
app = FastAPI()
@app.get("/")
def health():
    s = worker_status
    return JSONResponse({
        "status": "ok", "worker": s["status"],
        "checks": s["checks"], "renewals": s["renewals"],
        "skipped": s["skipped"], "failures": s["failures"],
        "starts": s["starts"],
    })


# ============================================================
# Gradio UI
# ============================================================
def make_page():
    s = worker_status
    rt = fmt_runtime(s.get("start_time"))

    status_map = {
        "running": ("🟢", "运行中", "#10b981", "#ecfdf5"),
        "waiting": ("🟡", "启动中", "#f59e0b", "#fffbeb"),
        "no_account": ("🔴", "未配置", "#ef4444", "#fef2f2"),
        "login_failed": ("🔴", "登录失败", "#ef4444", "#fef2f2"),
    }
    s_icon, s_text, s_color, s_bg = status_map.get(
        s["status"], ("⚪", s["status"], "#6b7280", "#f3f4f6")
    )

    ka_map = {
        "success": ("🛡️", "保活", "#3b82f6", "#eff6ff"),
        "failed": ("⚠️", "失败", "#ef4444", "#fef2f2"),
        "skipped": ("⏭️", "跳过", "#6b7280", "#f3f4f6"),
        None: ("⏳", "等待", "#f59e0b", "#fffbeb"),
    }
    k_icon, k_text, k_color, k_bg = ka_map.get(
        s.get("keepalive"), ("⏳", "等待", "#f59e0b", "#fffbeb")
    )

    # 服务器卡片
    server_cards = ""
    for sv in s.get("server_info", []):
        remain_s = sv.get("remain_seconds", -1)
        if remain_s < 0:
            bar_color = "#6b7280"
            bar_width = 0
            badge_color = "#6b7280"
            badge_bg = "#f3f4f6"
            badge_text = "未知"
        elif remain_s <= RENEW_THRESHOLD:
            bar_color = "#ef4444"
            bar_width = max(5, min(100, remain_s * 100 // max(RENEW_THRESHOLD, 1)))
            badge_color = "#ef4444"
            badge_bg = "#fef2f2"
            badge_text = "需续期"
        else:
            bar_color = "#10b981"
            bar_width = min(100, remain_s * 100 // 3600)
            badge_color = "#10b981"
            badge_bg = "#ecfdf5"
            badge_text = "正常"

        action_icon = "✅" if sv.get("success") else ("⏭️" if sv.get("last_action") == "跳过" else "❌")

        server_cards += f'''
        <div class="server-card">
            <div class="server-header">
                <span class="server-id">🖥️ {sv.get("id", "?")}</span>
                <span class="server-name">{sv.get("name", "Unknown")}</span>
                <span class="badge" style="background:{badge_bg};color:{badge_color}">{badge_text}</span>
            </div>
            <div class="server-bar-wrap">
                <div class="server-bar" style="width:{bar_width}%;background:{bar_color}"></div>
            </div>
            <div class="server-details">
                <span>⏱️ {sv.get("remaining", "--")}</span>
                <span>📅 {sv.get("expiry", "--")}</span>
                <span>{action_icon} {sv.get("last_action", "--")} @ {sv.get("time", "--")}</span>
                <span>👤 {sv.get("email", "--")}</span>
            </div>
        </div>'''

    if not server_cards:
        server_cards = '<div class="empty">⏳ 等待首次检查...</div>'

    logs = "\n".join(log_queue) if log_queue else "⏳ 等待日志..."

    return f'''
<div class="container">
    <div class="header">
        <div class="logo">🎮 Leme Host Renewal</div>
        <div class="badge" style="background:{s_bg};color:{s_color}">{s_icon} {s_text}</div>
        <div class="badge" style="background:{k_bg};color:{k_color}">{k_icon} {k_text}</div>
        <div class="stats-row">
            <span>🖥️ <b>{s["servers"]}</b></span>
            <span>🔄 <b>{s["renewals"]}</b></span>
            <span>⏭️ <b>{s["skipped"]}</b></span>
            <span>❌ <b>{s["failures"]}</b></span>
            <span>#{s["checks"]}</span>
            <span>⏱️ {rt}</span>
        </div>
    </div>

    <div class="info-bar">
        <span>📡 上次: <b>{s.get("last_check") or "--"}</b></span>
        <span>⏳ 下次: <b>{s.get("next_check") or "--"}</b></span>
        <span>🎯 阈值: <b>{RENEW_THRESHOLD}s ({RENEW_THRESHOLD // 60}分)</b></span>
        <span>🔁 间隔: <b>{CHECK_INTERVAL}s</b></span>
    </div>

    <div class="main">
        <div class="left-panel">
            <div class="card">
                <div class="card-title">🖥️ 服务器状态</div>
                <div class="server-list">{server_cards}</div>
            </div>
            <div class="card logs-card">
                <div class="card-title">📋 日志 <span class="sub">#{s["checks"]}</span></div>
                <div class="logs">{logs}</div>
            </div>
        </div>

        <div class="sidebar">
            <div class="card">
                <div class="card-title">📊 统计</div>
                <div class="stat-grid">
                    <div class="stat-item"><div class="stat-val">{s["accounts"]}</div><div class="stat-label">账号</div></div>
                    <div class="stat-item"><div class="stat-val">{s["servers"]}</div><div class="stat-label">服务器</div></div>
                    <div class="stat-item"><div class="stat-val green">{s["renewals"]}</div><div class="stat-label">续期</div></div>
                    <div class="stat-item"><div class="stat-val">{s["skipped"]}</div><div class="stat-label">跳过</div></div>
                    <div class="stat-item"><div class="stat-val red">{s["failures"]}</div><div class="stat-label">失败</div></div>
                    <div class="stat-item"><div class="stat-val">{s["checks"]}</div><div class="stat-label">检查</div></div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">⚙️ 环境变量</div>
                <div class="config-table">
                    <div class="config-row header-row">
                        <span class="config-name">变量名</span>
                        <span class="config-desc">说明</span>
                    </div>
                    <div class="config-row">
                        <code>LEME</code>
                        <span>账号密码 <b class="req">必填</b></span>
                    </div>
                    <div class="config-row">
                        <code>TG_BOT_TOKEN</code>
                        <span>TG 机器人 <b class="opt">可选</b></span>
                    </div>
                    <div class="config-row">
                        <code>TG_CHAT_ID</code>
                        <span>TG 聊天 ID <b class="opt">可选</b></span>
                    </div>
                    <div class="config-row">
                        <code>PROJECT_URL</code>
                        <span>保活链接 <b class="opt">可选</b></span>
                    </div>
                    <div class="config-row">
                        <code>CHECK_INTERVAL</code>
                        <span>检查间隔秒 <b class="opt">默认300</b></span>
                    </div>
                    <div class="config-row">
                        <code>RENEW_THRESHOLD</code>
                        <span>续期阈值秒 <b class="opt">默认900</b></span>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-title">📖 LEME 格式</div>
                <div class="help-steps">
                    <div class="url-example"><code>邮箱-----密码</code></div>
                    <div class="tip">多账号换行，5个短横线分隔<br><code>admin@example.com-----123456</code><br><code>user2@example.com-----abcdef</code></div>
                </div>
            </div>
        </div>
    </div>
</div>'''


CSS = """
#huggingface-space-header { display: none !important; }
footer { display: none !important; }
.gradio-container { background: #f8fafc !important; padding: 0 !important; }

.container { max-width: 1200px; margin: 0 auto; padding: 16px; font-family: system-ui, -apple-system, sans-serif; }

.header { display: flex; align-items: center; gap: 10px; padding: 12px 16px; background: white; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 10px; flex-wrap: wrap; }
.logo { font-size: 16px; font-weight: 700; color: #1e293b; }
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 4px 10px; border-radius: 16px; font-size: 11px; font-weight: 600; }
.stats-row { display: flex; gap: 12px; margin-left: auto; font-size: 12px; color: #64748b; flex-wrap: wrap; }
.stats-row b { color: #1e293b; }

.info-bar { display: flex; gap: 16px; padding: 8px 16px; background: white; border-radius: 8px; box-shadow: 0 1px 2px rgba(0,0,0,0.06); margin-bottom: 10px; font-size: 12px; color: #64748b; flex-wrap: wrap; }
.info-bar b { color: #374151; }

.main { display: grid; grid-template-columns: 1fr 300px; gap: 12px; }
@media (max-width: 800px) { .main { grid-template-columns: 1fr; } }

.card { background: white; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); overflow: hidden; margin-bottom: 12px; }
.card-title { padding: 10px 14px; font-size: 13px; font-weight: 600; color: #374151; border-bottom: 1px solid #e5e7eb; display: flex; }
.card-title .sub { margin-left: auto; color: #9ca3af; font-weight: 400; }

.server-list { padding: 8px; }
.server-card { padding: 10px 12px; border: 1px solid #e5e7eb; border-radius: 8px; margin-bottom: 8px; }
.server-card:last-child { margin-bottom: 0; }
.server-header { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.server-id { font-weight: 700; font-size: 13px; color: #1e293b; }
.server-name { font-size: 12px; color: #64748b; }
.server-bar-wrap { height: 4px; background: #e5e7eb; border-radius: 2px; margin-bottom: 6px; overflow: hidden; }
.server-bar { height: 100%; border-radius: 2px; transition: width 0.5s ease; }
.server-details { display: flex; gap: 12px; font-size: 11px; color: #64748b; flex-wrap: wrap; }

.empty { padding: 30px; text-align: center; color: #9ca3af; font-size: 13px; }

.logs { padding: 10px; font-family: 'SF Mono', Consolas, 'Courier New', monospace; font-size: 11px; line-height: 1.8; color: #475569; background: #f8fafc; height: 400px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }

.sidebar { display: flex; flex-direction: column; }

.stat-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; }
.stat-item { padding: 12px 8px; text-align: center; border-bottom: 1px solid #e5e7eb; border-right: 1px solid #e5e7eb; }
.stat-item:nth-child(3n) { border-right: none; }
.stat-item:nth-last-child(-n+3) { border-bottom: none; }
.stat-val { font-size: 20px; font-weight: 700; color: #1e293b; }
.stat-val.green { color: #10b981; }
.stat-val.red { color: #ef4444; }
.stat-label { font-size: 10px; color: #9ca3af; text-transform: uppercase; margin-top: 2px; }

.config-table { padding: 0; }
.config-row { display: flex; justify-content: space-between; align-items: center; padding: 7px 12px; border-bottom: 1px solid #f1f5f9; font-size: 11px; }
.config-row:last-child { border-bottom: none; }
.config-row.header-row { background: #f8fafc; font-weight: 600; color: #64748b; font-size: 10px; }
.config-row code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 10px; color: #1e293b; font-family: 'SF Mono', Consolas, monospace; }
.config-row .req { color: #ef4444; font-size: 9px; }
.config-row .opt { color: #6b7280; font-size: 9px; }

.help-steps { padding: 12px; }
.url-example { padding: 8px 12px; background: #f1f5f9; border-radius: 6px; border-left: 3px solid #3b82f6; margin-bottom: 8px; }
.url-example code { font-size: 12px; color: #3b82f6; font-family: 'SF Mono', Consolas, monospace; }
.tip { padding: 10px 12px; background: #fffbeb; border-radius: 6px; font-size: 11px; color: #92400e; line-height: 1.6; }
.tip code { background: #fef3c7; padding: 1px 4px; border-radius: 3px; font-size: 10px; }

.left-panel { display: flex; flex-direction: column; }
"""

with gr.Blocks(title="Leme Host Renewal", css=CSS, theme=gr.themes.Soft()) as gradio_app:
    html = gr.HTML(make_page)
    gr.Timer(5).tick(make_page, outputs=html)

app = gr.mount_gradio_app(app, gradio_app, path="/oyz")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
