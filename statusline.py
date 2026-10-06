#!/usr/bin/env python3
"""Claude Code 用マルチライン statusline。

表示内容:
  1行目: 5時間使用率 / コンテキストウィンドウ / gitブランチ
  2行目: 週間使用率（全モデル + モデル別。/weekly コマンドでON/OFF）
  3行目: model 名（alt+p で変更）
  4行目: effort レベル（alt+p → ←/→ で調整）
  5行目〜: 稼働中サブエージェント（作業ディレクトリ・コンテキスト・累計トークン・経過時間）

設定例（~/.claude/settings.json）:
  "statusLine": {
    "type": "command",
    "command": "python \"<このファイルへのパス>\"",
    "refreshInterval": 5
  }
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

BLUE = "\033[34m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
GREEN = "\033[32m"
MAGENTA = "\033[35m"
GRAY = "\033[90m"
RESET = "\033[0m"

WAITING = "5h使用率: データ取得待ち（最初のAPI応答後に表示されます）"

CONFIG_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
CACHE_DIR = os.path.join(CONFIG_DIR, "statusline-cache")
WEEKLY_FLAG = os.path.join(CONFIG_DIR, "statusline-weekly.on")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def read_input():
    """stdin を読む。空・BOM付き・不正JSON でも例外を投げず None を返す。"""
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        return None
    if not raw:
        return None
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def fmt_tokens(n):
    """トークン数を 60.7k / 1.0M の形に整形。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def bar(pct, width=14, color=BLUE):
    """pct(0-100) を width 文字のプログレスバーに。"""
    pct = max(0, min(100, pct))
    filled = max(0, min(width, round(width * pct / 100)))
    return f"{color}{'█' * filled}{GRAY}{'░' * (width - filled)}{RESET}"


def context_segment(data):
    """コンテキストウィンドウ表示。情報が無ければ空文字。"""
    cw = data.get("context_window") or {}
    used = cw.get("total_input_tokens")
    size = cw.get("context_window_size")
    if used is None or not size:
        return ""
    pct = cw.get("used_percentage")
    if pct is None:
        pct = round(used / size * 100)
    pct = int(round(pct))
    # 使用率に応じて色を変える（余裕→逼迫）
    color = BLUE if pct < 60 else (YELLOW if pct < 85 else RED)
    return f"{CYAN}コンテキスト{RESET} {bar(pct, color=color)} {fmt_tokens(used)} / {fmt_tokens(size)} ({pct}%)"


def five_hour_segment(rate):
    """5時間使用率表示（左端に置く塊）。"""
    five_hour = rate.get("five_hour") or {}
    pct = five_hour.get("used_percentage")
    resets_at = five_hour.get("resets_at")
    if pct is None:
        return WAITING
    pct_int = round(pct)
    reset_str = ""
    if resets_at:
        try:
            reset_str = f" (リセット {datetime.fromtimestamp(resets_at).strftime('%H:%M')})"
        except (ValueError, OSError, OverflowError):
            reset_str = ""
    return f"{GRAY}5h{RESET} {bar(pct_int)} {pct_int}%{reset_str}"


def find_branch(start_dir):
    """.git/HEAD を直読みしてブランチ名を得る（サブプロセス無し）。無ければ None。

    .git がファイルの場合（worktree/submodule）は gitdir: の指す先を辿る。
    """
    d = start_dir
    while d:
        g = os.path.join(d, ".git")
        head = None
        if os.path.isdir(g):
            head = os.path.join(g, "HEAD")
        elif os.path.isfile(g):
            try:
                t = open(g, encoding="utf-8").read().strip()
            except OSError:
                t = ""
            if t.startswith("gitdir:"):
                gd = t[len("gitdir:"):].strip()
                if not os.path.isabs(gd):
                    gd = os.path.normpath(os.path.join(d, gd))
                head = os.path.join(gd, "HEAD")
        if head:
            try:
                h = open(head, encoding="utf-8").read().strip()
            except OSError:
                return None
            if h.startswith("ref:"):
                ref = h[len("ref:"):].strip()
                return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            return h[:7] or None  # detached HEAD
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def oauth_usage():
    """OAuth使用量API（週間・モデル別週次）を5分キャッシュで取得。

    キャッシュにはトークンを入れず、表示に使う limits の要約だけ保存する。
    失敗時は60秒間リトライしない（statusline描画を遅延の連打から守る）。
    """
    cache_path = os.path.join(CACHE_DIR, "usage-oauth.json")
    now = time.time()
    cached = None
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
    except (OSError, ValueError):
        pass
    if cached and now - cached.get("fetched_at", 0) < 300:
        return cached
    if cached and now - cached.get("attempted_at", 0) < 60:
        return cached
    try:
        with open(os.path.join(CONFIG_DIR, ".credentials.json"), encoding="utf-8") as f:
            tok = json.load(f)["claudeAiOauth"]["accessToken"]
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            full = json.load(resp)
        slim = []
        for l in full.get("limits") or []:
            scope = ((l.get("scope") or {}).get("model") or {})
            slim.append({
                "kind": l.get("kind"),
                "percent": l.get("percent"),
                "resets_at": l.get("resets_at"),
                "name": scope.get("display_name"),
            })
        cached = {"fetched_at": now, "attempted_at": now, "limits": slim}
    except Exception:
        cached = cached or {}
        cached["attempted_at"] = now
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cached, f, ensure_ascii=False)
    except OSError:
        pass
    return cached


def _fmt_reset_iso(iso):
    """ISO日時 → '金 03:00' 形式。失敗したら空文字。"""
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return f"{'月火水木金土日'[dt.weekday()]} {dt.strftime('%H:%M')}"
    except (TypeError, ValueError):
        return ""


def weekly_line(rate):
    """週間制限の行。フラグOFF・情報なしなら None。/weekly でトグル。"""
    if not os.path.exists(WEEKLY_FLAG):
        return None
    rows = []
    reset_str = ""
    try:
        for l in (oauth_usage() or {}).get("limits") or []:
            if l.get("percent") is None:
                continue
            if l.get("kind") == "weekly_all":
                rows.append(("全モデル", int(round(l["percent"]))))
                reset_str = reset_str or _fmt_reset_iso(l.get("resets_at"))
            elif l.get("kind") == "weekly_scoped":
                rows.append((l.get("name") or "モデル別", int(round(l["percent"]))))
    except Exception:
        pass
    if not rows:
        # API不達時は statusline 入力の seven_day（全モデル）だけでも出す
        seven = (rate or {}).get("seven_day") or {}
        if seven.get("used_percentage") is None:
            return None
        rows.append(("全モデル", int(round(seven["used_percentage"]))))
        try:
            reset_str = f"{'月火水木金土日'[datetime.fromtimestamp(seven['resets_at']).weekday()]} " \
                        + datetime.fromtimestamp(seven["resets_at"]).strftime("%H:%M")
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            reset_str = ""
    segs = []
    for label, pct in rows:
        color = BLUE if pct < 60 else (YELLOW if pct < 85 else RED)
        segs.append(f"{label} {bar(pct, width=8, color=color)} {pct}%")
    line = f"{GRAY}週間{RESET} " + f" {GRAY}·{RESET} ".join(segs)
    if reset_str:
        line += f" {GRAY}(リセット {reset_str}){RESET}"
    line += f" {GRAY}(/weekly to hide){RESET}"
    return line


# ---------------------------------------------------------------------------
# サブエージェント表示
#   実体: <project>/<session_id>/subagents/agent-<id>.jsonl (+ .meta.json)
#   完了マーカーは親トランスクリプト側にのみ出る:
#     - background型: <task-id>ID</task-id> の task-notification
#     - 通常型: "tool_use_id":"toolu_..." の tool_result
#   statusline は毎描画で新プロセスなので、読み取り済みオフセットと完了判定を
#   ディスクキャッシュに持って増分スキャンする。
# ---------------------------------------------------------------------------

def _elapsed_str(seconds):
    s = max(0, int(seconds))
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"


def _shorten_path(p, limit=42):
    if not p:
        return "?"
    home = os.path.expanduser("~")
    if p.startswith(home):
        p = "~" + p[len(home):]
    if len(p) > limit:
        p = p[:18] + "…" + p[-(limit - 19):]
    return p


def _read_new_complete_lines(path, offset):
    """offset 以降の「改行で完結した行」だけ返す。(lines, new_offset)。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], offset
    if size < offset:
        offset = 0  # 作り直された
    if size == offset:
        return [], offset
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return [], offset
    nl = chunk.rfind(b"\n")
    if nl == -1:
        return [], offset  # 書き込み途中。次回に回す
    text = chunk[: nl + 1].decode("utf-8", errors="replace")
    return text.splitlines(), offset + nl + 1


def _update_agent_stats(st, agent_jsonl):
    """エージェント自身のjsonlを増分読みして ctx/累計/cwd を更新。"""
    lines, st["offset"] = _read_new_complete_lines(agent_jsonl, st.get("offset", 0))
    if st["offset"] == 0:
        st["cum"] = 0
        st["ctx"] = 0
    for ln in lines:
        if '"usage"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("cwd"):
            st["cwd"] = o["cwd"]
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(u, dict):
            continue
        inp = u.get("input_tokens") or 0
        cc = u.get("cache_creation_input_tokens") or 0
        cr = u.get("cache_read_input_tokens") or 0
        out = u.get("output_tokens") or 0
        st["cum"] = st.get("cum", 0) + inp + cc + out  # cache読取は除く＝実消費
        st["ctx"] = inp + cc + cr  # 直近のプロンプト側＝文脈サイズ
    if not st.get("cwd"):
        # usage行がまだ無い起動直後: 先頭行から cwd を拾う
        try:
            with open(agent_jsonl, encoding="utf-8", errors="replace") as f:
                first = f.readline()
            o = json.loads(first)
            if o.get("cwd"):
                st["cwd"] = o["cwd"]
        except (OSError, ValueError):
            pass
    return st


def agent_rows(data):
    """稼働中サブエージェントの行リストを返す。無ければ []。例外は上位で握る。"""
    sid = data.get("session_id")
    tp = data.get("transcript_path")
    if not sid or not tp:
        return []
    sub_dir = os.path.join(os.path.dirname(tp), sid, "subagents")
    if not os.path.isdir(sub_dir):
        return []
    metas = {}
    for name in os.listdir(sub_dir):
        if name.startswith("agent-") and name.endswith(".meta.json"):
            aid = name[len("agent-"):-len(".meta.json")]
            try:
                with open(os.path.join(sub_dir, name), encoding="utf-8") as f:
                    metas[aid] = json.load(f)
            except (OSError, ValueError):
                continue
    if not metas:
        return []

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"agents-{sid}.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    cache.setdefault("parent_offset", 0)
    cache.setdefault("done", [])
    cache.setdefault("agents", {})
    done = set(cache["done"])

    # 親トランスクリプトの新規行から完了マーカーを探す（未完了が居る時だけ）
    pending = {a: m for a, m in metas.items() if a not in done}
    if pending:
        lines, cache["parent_offset"] = _read_new_complete_lines(tp, cache["parent_offset"])
        if lines:
            blob = "\n".join(lines)
            for aid, meta in pending.items():
                if f"<task-id>{aid}</task-id>" in blob:
                    done.add(aid)
                    continue
                tuid = meta.get("toolUseId")
                is_bg = meta.get("requestShape") == "background"
                if tuid and not is_bg and re.search(
                    r'"tool_use_id"\s*:\s*"' + re.escape(tuid) + '"', blob
                ):
                    done.add(aid)
    cache["done"] = sorted(done)

    running = [a for a in metas if a not in done]
    rows = []
    for aid in sorted(running)[:6]:
        meta = metas[aid]
        st = cache["agents"].setdefault(aid, {"offset": 0, "cum": 0, "ctx": 0, "cwd": None})
        _update_agent_stats(st, os.path.join(sub_dir, f"agent-{aid}.jsonl"))
        try:
            started = os.path.getmtime(os.path.join(sub_dir, f"agent-{aid}.meta.json"))
            elapsed = _elapsed_str(time.time() - started)
        except OSError:
            elapsed = "?"
        name = meta.get("agentType") or "agent"
        desc = (meta.get("description") or "").strip()
        if len(desc) > 20:
            desc = desc[:20] + "…"
        rows.append(
            f"{GREEN}●{RESET} {name}"
            + (f" {GRAY}{desc}{RESET}" if desc else "")
            + f"  {_shorten_path(st.get('cwd'))}"
            + f" {GRAY}·{RESET} ctx {fmt_tokens(st.get('ctx'))}"
            + f" {GRAY}·{RESET} 累計 {fmt_tokens(st.get('cum'))}"
            + f" {GRAY}·{RESET} {elapsed}"
        )

    # 終了済みエージェントの統計は捨てて肥大を防ぐ
    cache["agents"] = {a: s for a, s in cache["agents"].items() if a in running}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        # 古いセッションのキャッシュを掃除（7日）
        now = time.time()
        for name in os.listdir(CACHE_DIR):
            p = os.path.join(CACHE_DIR, name)
            try:
                if now - os.path.getmtime(p) > 7 * 86400:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass
    return rows


def main():
    data = read_input()
    if not isinstance(data, dict):
        # 入力が空/不正でも落とさない（初回描画等で空 stdin が来ることがある）
        print(WAITING)
        return

    rate = data.get("rate_limits") or {}
    lines_out = []

    # 1行目: 5h使用率 | コンテキスト | branch
    ws = data.get("workspace") or {}
    branch = None
    try:
        branch = find_branch(ws.get("current_dir") or data.get("cwd") or "")
    except Exception:
        pass
    segs = [five_hour_segment(rate)]
    ctx = context_segment(data)
    if ctx:
        segs.append(ctx)
    segs.append(f"{GREEN}branch {branch or '-'}{RESET}")
    lines_out.append(f"{GRAY} | {RESET}".join(segs))

    # 週間制限（/weekly でON/OFF）
    try:
        wline = weekly_line(rate)
        if wline:
            lines_out.append(wline)
    except Exception:
        pass

    # model / effort（ショートカットヒント付き）
    model_name = (data.get("model") or {}).get("display_name")
    if model_name:
        fast = f" {YELLOW}⚡fast{RESET}" if data.get("fast_mode") else ""
        lines_out.append(
            f"{BLUE}model{RESET}  {CYAN}{model_name}{RESET}{fast} {GRAY}(alt+p to change){RESET}"
        )
    effort = (data.get("effort") or {}).get("level")
    if effort:
        lines_out.append(
            f"{MAGENTA}effort{RESET} {MAGENTA}{effort}{RESET} {GRAY}(alt+p then ←/→){RESET}"
        )

    # 稼働中サブエージェント
    try:
        lines_out.extend(agent_rows(data))
    except Exception:
        pass  # 表示系の失敗で statusline 全体を落とさない

    print("\n".join(lines_out))


if __name__ == "__main__":
    main()
