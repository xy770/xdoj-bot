#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XDOJ 自动答题机器人
- 每小时 AC 目标数：AC_PER_HOUR
- 每题只提交一次，失败即换下一题
- 失败的题冷却 24 小时
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

load_dotenv()


def env(key, default=None):
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"环境变量 {key} 未设置，请检查 .env 文件")
    return v


# ============================================================
# 配置
# ============================================================
BASE_URL    = env("XDOJ_BASE_URL", "http://47.92.139.74/xdoj-ssm")
CONTEST_ID  = env("XDOJ_CONTEST_ID", "11")
USER_ID     = env("XDOJ_USER_ID")
PASSWORD    = env("XDOJ_PASSWORD")
CLASS_ID    = env("XDOJ_CLASS_ID")
LANGUAGE_ID = env("XDOJ_LANGUAGE_ID", "2")

DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY")
DEEPSEEK_MODEL   = env("DEEPSEEK_MODEL", "deepseek-chat")

START_HOUR   = int(env("START_HOUR", "8"))
END_HOUR     = int(env("END_HOUR", "20"))
AC_PER_HOUR  = int(env("PROBLEMS_PER_HOUR", "2"))
MAX_ATTEMPTS = int(env("MAX_ATTEMPTS", "6"))
COOLDOWN_SEC = int(env("COOLDOWN_HOURS", "24")) * 3600

COMPILE_TIMEOUT = 30
RUN_TIMEOUT     = 5
POLL_TIMEOUT    = 180
POLL_INTERVAL   = 3

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
# 登录
# ============================================================
def login():
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] 登录中...")
    try:
        session.get(f"{BASE_URL}/", timeout=10)
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
        print("✔ 登录成功")
        return True
    except Exception as e:
        print(f"✘ 登录异常: {e}")
        return False


# ============================================================
# 题目列表
# ============================================================
def fetch_problem_list(page=1, rows=50):
    url = f"{BASE_URL}/contest/problems.do"
    params = {
        "contestId": CONTEST_ID, "userId": USER_ID, "_search": "false",
        "nd": int(time.time() * 1000), "rows": rows, "page": page,
        "sidx": "", "sord": "asc",
    }
    r = session.get(url, params=params, timeout=20, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": f"{BASE_URL}/team/problems.do",
    })
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
# PDF
# ============================================================
def fetch_problem_text(problem_id):
    url = f"{BASE_URL}/problem/viewTextFile.do"
    r = session.get(url, params={"problemId": problem_id}, timeout=30)
    r.raise_for_status()
    raw = r.content
    if raw.startswith(b"%PDF"):
        pdf_path = PROBLEMS_DIR / f"problem{problem_id}.pdf"
        pdf_path.write_bytes(raw)
        try:
            reader = PdfReader(str(pdf_path))
            return "\n".join((p.extract_text() or "") for p in reader.pages)
        except Exception as e:
            print(f"✘ PDF 解析失败: {e}")
            return ""
    else:
        return raw.decode("utf-8", errors="replace")


# ============================================================
# DeepSeek
# ============================================================
SYSTEM_PROMPT = '''你是一位资深算法竞赛选手和 C 语言专家。你必须一次性写出完全正确的 C 代码。

【输出格式】只输出 JSON，不要 markdown 代码块：
{"skip": false, "skip_reason": "", "code": "完整 C 代码", "sample_input": "样例输入", "sample_output": "样例输出", "reason": "算法思路"}

【判废题】题目内容为空、只有标题、写着"测试用/无有效题目/占位"时，返回 {"skip": true, "skip_reason": "原因"}。

【写代码前必做的思考】
1. 逐字读题面，重点看"输入说明"和"输出说明"里的每一个限制。
2. 识别关键词，主动做输入合法性检查：
   - "所有不合理输入返回 error" → 检查越界、重边、自环、负值、m 超过理论上限、格式错误
   - "多组数据/读到 EOF" → 用 while(scanf(...) != EOF)
   - "n 可达 10^5 / 10^6" → 避免 O(n^2) 空间，用邻接表而非邻接矩阵
   - "没有自环/无重边" 是输入保证，不必检查；但若另有"不合理输入返回 error"则必须查
3. 边界条件：n=0/1、空输入、负数、极大值、起点=终点、不连通、重复元素。
4. 输出格式：多余空格、末尾换行、小数位数、大小写（true/True/TRUE）。

【代码要求】
- 纯 C11，不要 C++ 特性
- 用 scanf/printf
- 不加任何注释（// 和 /* */ 都不要）
- 内存 O(n+m) 优先，不要动辄开 n*n 数组
- 时间 O(n+m) 或 O((n+m) log n)

【样例要求】
- sample_output 必须与 sample_input 严格对应
- 若题目给了样例，直接用它；若没给，自己造一个合理测试
'''


def ask_ai(problem_text):
    user_msg = "题目如下：\n\n" + problem_text + "\n"
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
# 本地编译测试
# ============================================================
def compile_and_test(code, sample_input, sample_output):
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / "main.c"
        exe = td / "main"
        src.write_text(code, encoding="utf-8")

        cmd = ["g++", "-O2", "-x", "c", "-std=c11", str(src), "-o", str(exe), "-lm"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "编译超时"
        if r.returncode != 0:
            return False, "编译错误：\n" + r.stderr

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
# 提交
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
    while time.time() - start < POLL_TIMEOUT:
        try:
            r = session.post(url, params=params, headers=headers, timeout=10)
            data = r.json().get("data", [])
            for item in data:
                if str(item.get("problemId")) == str(problem_id):
                    j = item.get("judgement")
                    txt = item.get("judgementText", "Unknown")
                    print(f"    轮询: {txt} (judgement={j})")
                    if j != 0:
                        return {"status": txt, "judgement": j}
                    break
        except Exception as e:
            print(f"    轮询异常: {e}")
        time.sleep(POLL_INTERVAL)
    return {"status": "Timeout", "judgement": -1}


# ============================================================
# State
# ============================================================
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "failed": {}, "skipped": [], "history": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================
# 处理单题（只提交一次）
# ============================================================
def process_one_problem(problem):
    pid = problem["id"]
    title = problem.get("title", "")
    print("\n" + "=" * 60)
    print(f"尝试题目 {pid} - {title}")
    print("=" * 60)

    try:
        problem_text = fetch_problem_text(pid)
    except Exception as e:
        print(f"✘ 获取题目失败: {e}")
        return False

    text = (problem_text or "").strip()
    if len(text) < 80:
        print(f"⚠️ 题目文本过短({len(text)})，判定废题")
        return "SKIP"

    keywords = ["输入", "输出", "样例", "Sample", "Input", "Output", "题目", "问题"]
    if not any(kw in text for kw in keywords):
        print("⚠️ 缺少题面关键词，判定废题")
        return "SKIP"

    (PROBLEMS_DIR / f"problem{pid}.txt").write_text(text, encoding="utf-8")
    print(f"题目文本长度: {len(text)}")

    try:
        result = ask_ai(text)
    except Exception as e:
        print(f"✘ AI 调用失败: {e}")
        return False

    attempt_dir = CODE_DIR / f"problem{pid}"
    attempt_dir.mkdir(exist_ok=True)
    (attempt_dir / "ai_response.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if result.get("skip") is True:
        print("⚠️ AI 判定无效题目：" + result.get("skip_reason", ""))
        return "SKIP"

    code       = (result.get("code") or "").strip()
    sample_in  = result.get("sample_input", "") or ""
    sample_out = result.get("sample_output", "") or ""
    print("AI 思路: " + result.get("reason", ""))

    if not code:
        print("✘ AI 未返回代码")
        return False

    ok, msg = compile_and_test(code, sample_in, sample_out)
    if not ok:
        print("✘ 本地测试失败（不提交）：")
        print(msg[:400])
        return False
    print("✔ 本地测试通过")

    print("→ 提交到 OJ...")
    submit_code(pid, code)

    print("→ 等待判题...")
    judge = poll_judgement(pid)
    print("判题结果: " + judge["status"])

    if judge["judgement"] == 1:
        print(f"✔✔ 题目 {pid} 通过！")
        (attempt_dir / "accepted.c").write_text(code, encoding="utf-8")
        return True
    print(f"✘ OJ 未通过：{judge['status']}")
    return False


# ============================================================
# 一轮
# ============================================================
def run_once():
    if not login():
        return
    state = load_state()
    done_set    = set(state.get("done", []))
    skipped_set = set(state.get("skipped", []))
    failed      = state.get("failed", {})
    now_ts      = time.time()

    cooldown_ids = set()
    for pid_str, info in failed.items():
        if isinstance(info, dict):
            if now_ts - info.get("last_time", 0) < COOLDOWN_SEC:
                try:
                    cooldown_ids.add(int(pid_str))
                except ValueError:
                    pass

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 获取题目列表...")
    problems = fetch_all_problems()
    pending = [p for p in problems
               if p["id"] not in done_set
               and p["id"] not in skipped_set
               and p["id"] not in cooldown_ids]
    print(f"共 {len(problems)} 题 | 已完成 {len(done_set)} | 废题 {len(skipped_set)} "
          f"| 冷却中 {len(cooldown_ids)} | 待处理 {len(pending)}")
    print(f"目标：本小时 AC {AC_PER_HOUR} 道，最多尝试 {MAX_ATTEMPTS} 道\n")

    ac_count = 0
    attempted = 0

    while ac_count < AC_PER_HOUR and pending and attempted < MAX_ATTEMPTS:
        attempted += 1
        p = pending.pop(0)
        try:
            result = process_one_problem(p)
        except Exception as e:
            print(f"✘ 处理题目 {p['id']} 异常: {e}")
            result = False

        if result == "SKIP":
            skipped_set.add(p["id"])
            state["skipped"] = sorted(skipped_set)
        elif result is True:
            done_set.add(p["id"])
            state["done"] = sorted(done_set)
            ac_count += 1
            print(f"\n✔ 本小时进度: AC {ac_count}/{AC_PER_HOUR}（已尝试 {attempted} 道）")
        else:
            info = failed.get(str(p["id"]), {})
            cnt = info.get("count", 0) if isinstance(info, dict) else 0
            failed[str(p["id"])] = {"count": cnt + 1, "last_time": now_ts}

        state["failed"] = failed
        state.setdefault("history", []).append({
            "id": p["id"], "title": p.get("title"),
            "time": datetime.now().isoformat(),
            "status": "ok" if result is True else ("skip" if result == "SKIP" else "fail"),
        })
        save_state(state)

    if ac_count >= AC_PER_HOUR:
        print(f"\n🎉 本小时完成 {ac_count} 道 AC")
    else:
        print(f"\n⚠️ 本小时只完成 {ac_count} 道 AC（尝试了 {attempted} 道）")


# ============================================================
# 调度器
# ============================================================
def scheduler():
    print(f"启动调度：每天 {START_HOUR}:00 - {END_HOUR}:59，每小时 AC {AC_PER_HOUR} 道")
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
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 休眠至 {nxt.strftime('%H:%M:%S')}")
        time.sleep(sleep_secs)


# ============================================================
# 入口
# ============================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="跑一轮后退出")
    ap.add_argument("--problem-id", type=int, help="只处理指定题目")
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
        if ok is True:
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
