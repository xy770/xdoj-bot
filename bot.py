#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XDOJ 自动答题机器人（正式版）
- 每天 8:00~20:59，每小时处理 N 道未做的题
- 登录 → selectContest → 拉题 → DeepSeek 生成 C 代码
- → g++ 本地编译 + 样例验证 → 提交 → 轮询判题 → 失败重写
"""

import os
import re
import json
import time
import argparse
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import requests
from openai import OpenAI
from dotenv import load_dotenv

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

# 加载 .env 文件（如果存在）
load_dotenv()


def env(key, default=None):
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"环境变量 {key} 未设置，请检查 .env 文件")
    return v


# ============================================================
# 配置区（从 .env 读取）
# ============================================================
BASE_URL    = env("XDOJ_BASE_URL", "http://47.92.139.74/xdoj-ssm")
CONTEST_ID  = env("XDOJ_CONTEST_ID", "11")
USER_ID     = env("XDOJ_USER_ID")
PASSWORD    = env("XDOJ_PASSWORD")
CLASS_ID    = env("XDOJ_CLASS_ID")
LANGUAGE_ID = env("XDOJ_LANGUAGE_ID", "2")

DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL   = env("DEEPSEEK_MODEL", "deepseek-chat")

START_HOUR        = int(env("START_HOUR", "8"))
END_HOUR          = int(env("END_HOUR", "20"))
PROBLEMS_PER_HOUR = int(env("PROBLEMS_PER_HOUR", "2"))
MAX_RETRIES       = 3
COMPILE_TIMEOUT   = 30
RUN_TIMEOUT       = 5
POLL_TIMEOUT      = 180
POLL_INTERVAL     = 3

WORK_DIR     = Path("./work")
PROBLEMS_DIR = WORK_DIR / "problems"
CODE_DIR     = WORK_DIR / "code"
STATE_FILE   = WORK_DIR / "state.json"
for d in (PROBLEMS_DIR, CODE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# ============================================================
# Session
# ============================================================
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:157.0) Gecko/20100101 Firefox/157.0",
    "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,zh-HK;q=0.7,en-US;q=0.6,en;q=0.5",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
})
session.cookies.set("preferred_lang", "zh_CN")

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


# ============================================================
# 登录 + selectContest
# ============================================================
def login():
    print(f"\n[{datetime.now():%H:%M:%S}] 登录中...")
    try:
        # 1) 访问首页，拿到初始 JSESSIONID
        session.get(f"{BASE_URL}/", timeout=10)

        # 2) 登录
        r = session.post(
            f"{BASE_URL}/user/login.do",
            data={"userId": USER_ID, "password": PASSWORD, "preferLanguage": "zh_CN"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/",
            },
            timeout=10,
        )
        if "JSESSIONID" not in session.cookies:
            print("✘ 登录失败：未拿到 JSESSIONID")
            return False

        # 3) selectContest：把 contest 对象放进 session
        session.post(
            f"{BASE_URL}/team/selectContest.do",
            data={"contestId": CONTEST_ID},
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/team/contests.do",
            },
            timeout=10,
        )
        print(f"✔ 登录成功，JSESSIONID = {session.cookies.get('JSESSIONID')[:16]}...")
        return True
    except Exception as e:
        print(f"✘ 登录异常: {e}")
        return False


# ============================================================
# 拉题目列表
# ============================================================
def fetch_problem_list(page=1, rows=50):
    url = f"{BASE_URL}/contest/problems.do"
    params = {
        "contestId": CONTEST_ID, "userId": USER_ID, "_search": "false",
        "nd": int(time.time() * 1000), "rows": rows, "page": page,
        "sidx": "", "sord": "asc",
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE_URL}/team/problems.do",
    }
    r = session.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_all_problems():
    problems, page = [], 1
    while True:
        data = fetch_problem_list(page=page, rows=50)
        items = data.get("data") or []
        if not items:
            break
        problems.extend(items)
        if page >= int(data.get("totalpages", 1)):
            break
        page += 1
    return problems


# ============================================================
# 下载 PDF 题目
# ============================================================
def fetch_problem_text(problem_id):
    url = f"{BASE_URL}/problem/viewTextFile.do"
    r = session.get(url, params={"problemId": problem_id}, timeout=30)
    r.raise_for_status()
    raw = r.content
    if raw.startswith(b"%PDF"):
        pdf_path = PROBLEMS_DIR / f"problem{problem_id}.pdf"
        pdf_path.write_bytes(raw)
        return extract_pdf_text(pdf_path)
    else:
        return raw.decode("utf-8", errors="replace")


def extract_pdf_text(pdf_path):
    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        print(f"✘ PDF 解析失败: {e}")
        return ""


# ============================================================
# DeepSeek 生成代码
# ============================================================
SYSTEM_PROMPT = """你是一位资深算法竞赛选手和 C 语言专家，请根据题目描述编写正确的 C 语言代码。

严格要求：
1. 只输出 JSON，不要有任何额外文字、不要 markdown 代码块标记。
2. JSON 字段：
   - "skip"          : 布尔值。如果题目无效，设为 true 并忽略下面所有字段。
   - "skip_reason"   : skip=true 时，简述为什么该题目无效。
   - "code"          : 完整的 C 语言源代码（字符串）
   - "sample_input"  : 题目给出的样例输入（字符串）
   - "sample_output" : 对应的正确输出（字符串）
   - "reason"        : 简要算法思路
3. 什么情况判定为无效题目（skip=true）：
   - 题面完全为空，或只有标题、页码、乱码
   - 题面明确写着"测试用"、"测试题"、"样例"、"没有有效题目"、"题目无效"、"占位"等
   - 题面没有任何可解题的描述（无输入/输出说明、无样例）
   - 题面自相矛盾或残缺不全无法推断任务
4. 有效的题目才需要写代码，此时要求：
   - 纯 C 语言（C11），不要用 C++ 特性。
   - 用 scanf / printf 标准输入输出。
   - 效率高、内存占用小。
   - 处理边界情况（n=0、空输入、负数、大数、多组数据）。
   - 输出格式严格匹配（空格、换行、小数位数等）。
   - **代码中不允许有任何注释**（// 和 /* */ 都不要）。
5. sample_output 必须与 sample_input 严格对应。
"""


def ask_ai(problem_text, error_feedback=None):
    user_msg = f"题目如下：\n\n{problem_text}\n"
    if error_feedback:
        user_msg += f"\n上一次尝试失败，错误信息如下，请修正后重新给出完整代码：\n{error_feedback}\n"

    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.2,
        response_format={"type": "json_object"},
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    return json.loads(content)


# ============================================================
# 本地编译 + 样例验证
# ============================================================
def compile_and_test(code, sample_input, sample_output):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "main.c"
        exe = td / "main"
        src.write_text(code, encoding="utf-8")

        cmd = ["g++", "-O2", "-x", "c", "-std=c11", str(src), "-o", str(exe), "-lm"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "编译超时"
        if r.returncode != 0:
            return False, f"编译错误：\n{r.stderr}"

        stdin = sample_input or ""
        if stdin and not stdin.endswith("\n"):
            stdin += "\n"
        try:
            r = subprocess.run([str(exe)], input=stdin, capture_output=True,
                               text=True, timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"运行超时（>{RUN_TIMEOUT}s）"

        if r.returncode != 0:
            return False, f"运行时错误 exit={r.returncode}\nstderr={r.stderr}"

        got = r.stdout.strip()
        exp = (sample_output or "").strip()
        if got != exp:
            return False, f"输出不匹配\n期望：\n{exp}\n实际：\n{got}"
        return True, got


# ============================================================
# 提交代码
# ============================================================
def submit_code(problem_id, code):
    url = f"{BASE_URL}/submission/submit.do"
    payload = {
        "solutionType": (None, "0"),
        "contestId":    (None, CONTEST_ID),
        "userId":       (None, USER_ID),
        "classId":      (None, CLASS_ID),
        "problemId":    (None, str(problem_id)),
        "languageId":   (None, LANGUAGE_ID),
        "sourceFile":   ("main.c", code, "text/x-csrc"),
        "sourceCode":   (None, code),
    }
    headers = {
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/team/submit.do?problemId={problem_id}",
    }
    try:
        resp = session.post(url, files=payload, headers=headers, timeout=30)
        return resp.text[:300]
    except Exception as e:
        return f"提交异常: {e}"


# ============================================================
# 轮询判题结果
# ============================================================
def poll_judgement(problem_id):
    url = f"{BASE_URL}/submission/results.do"
    params = {
        "contestId": CONTEST_ID, "userId": USER_ID,
        "page": 1, "rows": 10,
        "sidx": "id", "sord": "desc",
        "_search": "false", "nd": int(time.time() * 1000),
    }
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE_URL}/team/results.do",
    }
    start = time.time()
    latest = None
    while time.time() - start < POLL_TIMEOUT:
        try:
            r = session.post(url, params=params, headers=headers, timeout=10)
            data = r.json().get("data", [])
            for item in data:
                if str(item.get("problemId")) == str(problem_id):
                    latest = item
                    break
            if latest:
                j = latest.get("judgement")
                text = latest.get("judgementText", "Unknown")
                print(f"    轮询: {text} (judgement={j})")
                if j != 0:
                    return {"status": text, "judgement": j}
        except Exception as e:
            print(f"    轮询异常: {e}")
        time.sleep(POLL_INTERVAL)
    return {"status": "Timeout", "judgement": -1}


# ============================================================
# 状态管理
# ============================================================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "failed": {}, "history": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                          encoding="utf-8")


# ============================================================
# 处理单题
# ============================================================
def process_one_problem(problem):
    pid = problem["id"]
    title = problem.get("title", "")
    print(f"\n{'='*60}\n处理题目 {pid} - {title}\n{'='*60}")

    try:
        problem_text = fetch_problem_text(pid)
    except Exception as e:
        print(f"✘ 获取题目失败: {e}")
        return False

    if not problem_text or len(problem_text.strip()) < 5:
        print("✘ 题目文本为空，跳过")
        return False
    print(f"题目文本长度: {len(problem_text)}")

    error_feedback = None
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"\n--- 第 {attempt}/{MAX_RETRIES} 次尝试 ---")
        try:
            result = ask_ai(problem_text, error_feedback)
        except Exception as e:
            print(f"✘ AI 调用失败: {e}")
            error_feedback = f"AI 调用异常：{e}"
            time.sleep(2)
            continue

        if result.get("skip") is True:
            reason = result.get("skip_reason", "AI 判定题目无效")
            print(f"⚠️ AI 判定为无效题目：{reason}")
            return "SKIP"

        code       = (result.get("code") or "").strip()
        sample_in  = result.get("sample_input", "") or ""
        sample_out = result.get("sample_output", "") or ""

        print(f"AI 思路: {result.get('reason', '')}")
        if not code:
            error_feedback = "AI 未返回 code 字段"
            continue

        ok, msg = compile_and_test(code, sample_in, sample_out)
        if not ok:
            print(f"✘ 本地测试失败: {msg}")
            error_feedback = msg
            continue
        print(f"✔ 本地测试通过，输出: {msg!r}")

        # 提交
        print("→ 提交到 OJ...")
        sub_result = submit_code(pid, code)
        print(f"提交响应: {sub_result}")

        # 轮询
        print("→ 等待判题...")
        judge = poll_judgement(pid)
        print(f"判题结果: {judge['status']}")

        if judge["judgement"] == 1:
            print(f"✔✔ 题目 {pid} 通过！")
            (CODE_DIR / f"problem{pid}.c").write_text(code, encoding="utf-8")
            return True
        else:
            error_feedback = (f"OJ 判题结果: {judge['status']}。"
                              f"请检查边界条件、输出格式或算法复杂度。")
            print(f"✘ OJ 未通过：{judge['status']}")

    print(f"✘ 题目 {pid} 重试 {MAX_RETRIES} 次后仍失败")
    return False


# ============================================================
# 一轮：每小时做 N 道题
# ============================================================
def run_once():
    if not login():
        return
    state = load_state()
    done_set = set(state.get("done", []))

    print(f"\n[{datetime.now():%H:%M:%S}] 获取题目列表...")
    problems = fetch_all_problems()
    pending = [p for p in problems if p["id"] not in done_set]
    print(f"共 {len(problems)} 题，已完成 {len(done_set)}，待处理 {len(pending)}")

    if not pending:
        print("没有待处理题目")
        return

    for _ in range(PROBLEMS_PER_HOUR):
        if not pending:
            break
        p = pending.pop(0)
        try:
            ok = process_one_problem(p)
        except Exception as e:
            print(f"✘ 处理题目 {p['id']} 异常: {e}")
            ok = False

        if ok:
            done_set.add(p["id"])
            state["done"] = sorted(done_set)
        else:
            fails = state.setdefault("failed", {})
            fails[str(p["id"])] = fails.get(str(p["id"]), 0) + 1

        state.setdefault("history", []).append({
            "id": p["id"], "title": p.get("title"),
            "time": datetime.now().isoformat(),
            "status": "ok" if ok else "fail",
        })
        save_state(state)


# ============================================================
# 调度器
# ============================================================
def scheduler():
    print(f"启动调度：每天 {START_HOUR}:00 - {END_HOUR}:59，每小时 {PROBLEMS_PER_HOUR} 道")
    while True:
        now = datetime.now()
        if START_HOUR <= now.hour <= END_HOUR:
            try:
                run_once()
            except Exception as e:
                print(f"✘ 本轮执行异常: {e}")

        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        sleep_secs = max(5, (next_hour - datetime.now()).total_seconds())
        nxt = datetime.now() + timedelta(seconds=sleep_secs)
        print(f"\n[{datetime.now():%H:%M:%S}] 休眠至 {nxt:%H:%M:%S}")
        time.sleep(sleep_secs)


# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--problem-id", type=int, help="只做指定题目")
    ap.add_argument("--list", action="store_true", help="只拉题目列表")
    args = ap.parse_args()

    if args.list:
        if login():
            problems = fetch_all_problems()
            print(f"共 {len(problems)} 题")
            for p in problems[:10]:
                print(f"  ID={p['id']}, Title={p['title']}")
        return

    if args.problem_id:
        if not login():
            return
        problems = fetch_all_problems()
        target = next((p for p in problems if p["id"] == args.problem_id), None)
        if not target:
            print(f"未找到题目 {args.problem_id}")
            return
        ok = process_one_problem(target)
        if ok:
            state = load_state()
            done = set(state.get("done", []))
            done.add(args.problem_id)
            state["done"] = sorted(done)
            save_state(state)
        return

    if args.once:
        run_once()
        return

    scheduler()


if __name__ == "__main__":
    main()
