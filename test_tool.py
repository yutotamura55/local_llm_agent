from __future__ import annotations

import csv
import difflib
import json
import os
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path

import ollama

# ===== 検証用パラメータ（ここを変えてモデル比較する） =====
MODEL = "qwen2.5-coder:7b"  # 例: "qwen2.5-coder:14b" などに差し替えて比較
TASK_LABEL = "test execute_command"  # ログ上でどのタスクの実験か区別するラベル
USER_PROMPT = "現在のディレクトリのファイル一覧をlsコマンドで確認して"

# True: annotate_commentsで前処理してからモデルに渡す（回避策あり）
# False: 生のファイル内容をそのまま渡す（モデル本来の理解力を検証）
USE_COMMENT_ANNOTATION = False

LOG_FILE = Path(__file__).parent / "verification_log.csv"

# execute_command のデフォルトタイムアウト秒数
DEFAULT_TIMEOUT_SEC = 30

# execute_command: 読み取り専用とみなすコマンド(先頭トークンで判定)。
# ここに載っているコマンドは confirm なしで即実行する。
READONLY_COMMANDS = {
    "ls",
    "cat",
    "pwd",
    "echo",
    "grep",
    "find",
    "head",
    "tail",
    "wc",
    "git",  # 基本読み取り系だが、書き込みサブコマンドは下記で個別に除外する
    "pip",  # list/show 系のみ許可。install等は書き込み判定に回る
    "npm",  # list/show 系のみ許可。install等は書き込み判定に回る
}

# 上記のうち、サブコマンドレベルで「書き込み」とみなすものは
# READONLY_COMMANDS に載っていても confirm 対象にする。
WRITE_SUBCOMMANDS = {
    "git": {
        "commit",
        "push",
        "checkout",
        "reset",
        "merge",
        "rebase",
        "add",
        "rm",
        "clean",
        "stash",
        "tag",
        "branch",
    },
    "pip": {"install", "uninstall"},
    "npm": {"install", "uninstall", "run", "ci"},
}


# LLMに渡す「道具」を定義する
def list_files(directory: str) -> str:
    """指定したディレクトリのファイル一覧を返す"""
    try:
        files = os.listdir(directory)
        return "\n".join(files)
    except Exception as e:
        return f"エラー: {e}"


def read_file(path: str) -> str:
    """指定したファイルの中身を読んで返す"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # USE_COMMENT_ANNOTATION=Trueの時だけ、シェル系ファイルに注釈を付けて渡す
        # False時は生の内容をそのまま渡し、モデル単体の理解力を検証する
        if USE_COMMENT_ANNOTATION and path.endswith(
            (".bashrc", ".bash_profile", ".sh", ".zshrc")
        ):
            content = annotate_comments(content)
        return content
    except FileNotFoundError:
        return f"エラー: ファイルが見つかりません: {path}"
    except UnicodeDecodeError:
        return f"エラー: テキストとして読めないファイルです(画像やバイナリの可能性): {path}"
    except Exception as e:
        return f"エラー: {e}"


def write_file(path: str, content: str) -> str:
    """指定したファイルに内容を書き込む(新規作成 or 上書き)"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功: {path} に書き込みました"
    except Exception as e:
        return f"エラー: {e}"


def _is_readonly_command(command: str) -> bool:
    """コマンド文字列が読み取り専用とみなせるか判定する。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        # クォート不整合などパースできない場合は安全側に倒しconfirm対象にする
        return False

    if not tokens:
        return False

    # パイプ・リダイレクト・連結・コマンド置換が含まれる場合は
    # 全体としての安全性を保証できないためconfirm対象にする
    if any(op in command for op in (">", ">>", "|", "&&", ";", "<", "`", "$(")):
        return False

    head = tokens[0]
    if head not in READONLY_COMMANDS:
        return False

    write_subs = WRITE_SUBCOMMANDS.get(head)
    if write_subs and len(tokens) > 1 and tokens[1] in write_subs:
        return False

    return True


def execute_command(command: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> str:
    """シェルコマンドを実行する(呼び出し前のconfirmは呼び出し側で処理する)"""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"エラー: コマンドがタイムアウトしました({timeout}秒): {command}"
    except Exception as e:
        return f"エラー: {e}"

    parts = []
    if proc.stdout:
        parts.append(proc.stdout.rstrip("\n"))
    if proc.stderr:
        parts.append(f"[stderr]\n{proc.stderr.rstrip(chr(10))}")
    parts.append(f"[exit code: {proc.returncode}]")
    return "\n".join(parts)


def edit_file(path: str, search: str, replace: str) -> str:
    """ファイル内のテキストを厳密一致・単一置換で書き換える。
    search がファイル内で一意でない場合(0回・複数回)はエラーを返す。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        return f"エラー: ファイルが見つかりません: {path}"
    except Exception as e:
        return f"エラー: {e}"

    count = original.count(search)
    if count == 0:
        return "エラー: search文字列がファイル内に見つかりませんでした。完全に一致する文字列を指定してください。"
    if count > 1:
        return f"エラー: search文字列がファイル内に{count}箇所存在し一意に特定できません。前後の文脈を含めて一意になるようsearchを広げてください。"

    updated = original.replace(search, replace, 1)

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(updated)
    except Exception as e:
        return f"エラー: 書き込みに失敗しました: {e}"

    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )
    return f"成功: {path} を更新しました\n{diff}"


def annotate_comments(content: str) -> str:
    """コメント行(#で始まる行)に「実行されない」という事実だけを付記する"""
    lines = content.split("\n")
    annotated = []
    for line in lines:
        if line.strip().startswith("#"):
            annotated.append(f"[NOT EXECUTED] {line}")
        else:
            annotated.append(line)
    return "\n".join(annotated)


def log_call(
    model: str, task_label: str, wall_time: float, ollama_response: dict, note: str = ""
):
    """1回のollama呼び出しの実行時間をCSVに追記する"""
    eval_count = ollama_response.get("eval_count", 0) or 0
    eval_duration = ollama_response.get("eval_duration", 0) or 0
    tokens_per_sec = (eval_count / (eval_duration / 1e9)) if eval_duration else 0.0

    row = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "task": task_label,
        "comment_annotation": USE_COMMENT_ANNOTATION,
        "note": note,
        "wall_time_sec": round(wall_time, 3),
        "total_duration_sec": round(
            (ollama_response.get("total_duration", 0) or 0) / 1e9, 3
        ),
        "load_duration_sec": round(
            (ollama_response.get("load_duration", 0) or 0) / 1e9, 3
        ),
        "prompt_eval_count": ollama_response.get("prompt_eval_count", 0),
        "eval_count": eval_count,
        "tokens_per_sec": round(tokens_per_sec, 1),
    }

    is_new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=row.keys())
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def timed_chat(**kwargs) -> tuple[dict, float]:
    """ollama.chatを実行し、壁時計時間も一緒に返す"""
    start = time.perf_counter()
    response = ollama.chat(**kwargs)
    wall_time = time.perf_counter() - start
    return response, wall_time


system_prompt = {
    "role": "system",
    "content": "あなたはファイル操作を手伝うアシスタントです。",
}

tools = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "指定したディレクトリ内のファイル一覧を取得する",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "一覧を取得したいディレクトリのパス",
                    }
                },
                "required": ["directory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "指定したファイルの中身をテキストとして読み込む",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "読み込みたいファイルのパス",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "指定したファイルに内容を書き込む(新規作成、または既存ファイルの上書き)",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "書き込み先のファイルパス",
                    },
                    "content": {"type": "string", "description": "書き込む内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_command",
            "description": (
                "シェルコマンドを実行する。読み取り専用コマンド(ls, cat, git status等)は"
                "確認なしで実行されるが、変更を伴う可能性のあるコマンドはユーザー確認が必要。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "実行するコマンド"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "ファイル内のテキストを厳密一致・単一置換で書き換える。"
                "search文字列はファイル内で一意でなければならない。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "対象ファイルのパス"},
                    "search": {
                        "type": "string",
                        "description": "置換対象の完全一致文字列(ファイル内で一意である必要がある)",
                    },
                    "replace": {"type": "string", "description": "置換後の文字列"},
                },
                "required": ["path", "search", "replace"],
            },
        },
    },
]


def extract_fallback_tool_call(message) -> list | None:
    """
    一部モデル(qwen2.5-coder系など)はtool_callsを使わず、
    contentに生のJSON文字列としてツール呼び出しを出力することがある。
    そのケースを検知し、通常のtool_callsと同じ形の擬似オブジェクトに変換する。
    """
    content = (message.content or "").strip()
    if not content:
        return None
    # ```json ... ``` のコードブロックで囲まれているケースも剥がす
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None

    name = parsed.get("name")
    arguments = parsed.get("arguments", {})
    if not name:
        return None

    class _FallbackFunction:
        def __init__(self, name, arguments):
            self.name = name
            self.arguments = arguments

    class _FallbackToolCall:
        def __init__(self, name, arguments):
            self.function = _FallbackFunction(name, arguments)

    return [_FallbackToolCall(name, arguments)]


response, wall_time = timed_chat(
    model=MODEL,
    messages=[
        system_prompt,
        {"role": "user", "content": USER_PROMPT},
    ],
    tools=tools,
)
log_call(MODEL, TASK_LABEL, wall_time, response, note="1st call (tool selection)")

print(response["message"])

message = response["message"]

tool_calls = message.tool_calls
used_fallback_parsing = False
if not tool_calls:
    tool_calls = extract_fallback_tool_call(message)
    if tool_calls:
        used_fallback_parsing = True
        print(
            "[注意] tool_callsが空だったため、content内の生JSONをフォールバックでパースしました"
        )

if tool_calls:
    for tool_call in tool_calls:
        name = tool_call.function.name
        args = tool_call.function.arguments

        if name == "list_files":
            real_path = os.path.expanduser("~")
            result = list_files(real_path)
        elif name == "read_file":
            # モデルの創作パスは使わず、実在するファイルを決め打ちで指定
            real_path = os.path.expanduser("~/.bashrc")
            result = read_file(real_path)
        elif name == "write_file":
            target_path = os.path.expanduser(
                "~/test_output.txt"
            )  # 安全のため専用の練習用ファイルに固定
            content_preview = args.get("content", "")

            print("\n--- 確認 ---")
            print(f"次の内容を {target_path} に書き込みます:")
            print(content_preview)
            confirm = input("実行しますか？ (y/n): ")

            if confirm.lower() == "y":
                result = write_file(target_path, content_preview)
            else:
                result = "ユーザーが書き込みをキャンセルしました"
        elif name == "execute_command":
            command = args.get("command", "")

            if _is_readonly_command(command):
                result = execute_command(command)
            else:
                print("\n--- 確認 ---")
                print(f"次のコマンドは変更を伴う可能性があります:")
                print(f"  {command}")
                confirm = input("実行しますか？ (y/n): ")

                if confirm.lower() == "y":
                    result = execute_command(command)
                else:
                    result = "ユーザーがコマンド実行をキャンセルしました"
        elif name == "edit_file":
            target_path = args.get("path", "")
            search = args.get("search", "")
            replace = args.get("replace", "")

            # プレビュー用に事前に一意性とdiffだけ確認する
            try:
                with open(target_path, "r", encoding="utf-8") as f:
                    original = f.read()
                count = original.count(search)
            except Exception as e:
                original = None
                count = 0

            if original is None:
                result = (
                    f"エラー: ファイルが見つからないか読み込めません: {target_path}"
                )
            elif count != 1:
                result = (
                    f"エラー: search文字列が一意に特定できません(該当箇所: {count})"
                )
            else:
                preview = original.replace(search, replace, 1)
                diff = "\n".join(
                    difflib.unified_diff(
                        original.splitlines(),
                        preview.splitlines(),
                        fromfile=target_path,
                        tofile=target_path,
                        lineterm="",
                    )
                )
                print("\n--- 確認 ---")
                print(f"次の変更を {target_path} に適用します:")
                print(diff)
                confirm = input("実行しますか？ (y/n): ")

                if confirm.lower() == "y":
                    result = edit_file(target_path, search, replace)
                else:
                    result = "ユーザーが編集をキャンセルしました"
        else:
            result = f"エラー: 未対応の道具です: {name}"

        print(f"--- {name} 実行結果 ---")
        print(result)

        follow_up, follow_up_wall_time = timed_chat(
            model=MODEL,
            messages=[
                system_prompt,
                {"role": "user", "content": USER_PROMPT},
                message,
                {"role": "tool", "content": result, "tool_name": name},
            ],
        )
        note = "2nd call (follow-up)"
        if used_fallback_parsing:
            note += " [fallback tool_call parsing]"
        log_call(MODEL, TASK_LABEL, follow_up_wall_time, follow_up, note=note)

        print("--- 最終回答 ---")
        print(follow_up["message"]["content"])
else:
    print("[警告] tool_callsが取得できず、フォールバックパースも失敗しました。")
    print("モデルの出力内容:")
    print(message.content)

print(f"\n実行ログを {LOG_FILE} に追記しました。")
