"""
エージェントが使うツール関数群。

test_tool.py との違い:
  - input() による確認は行わない(呼び出し側=APIレイヤーがconfirm状態を管理する)
  - edit_file / execute_command は「プレビュー生成」と「実行」を分離した関数を提供する

パスの安全性について:
  - モデルが渡すパスは信用しない前提を維持するため、ファイル操作系のツールは
    すべて base_dir (許可されたワークスペースのルート) を受け取り、
    resolve_safe_path() で「base_dir配下に実際に収まっているか」を検証してから実行する。
  - シンボリックリンク経由の迂回も防ぐため os.path.realpath で実体パスまで解決してから判定する。
  - execute_command はシェルコマンド全体を実行するツールの性質上、パス単位の検証では
    完全なサンドボックス化はできない(例: `cat /etc/passwd` は防げない)。
    現状は cwd を base_dir に固定するところまでに留めている。
"""

from __future__ import annotations

import difflib
import os
import shlex
import subprocess
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# 許可されたワークスペース(BASE_DIR)の一覧
# ブラウザからは自由入力させず、ここで事前に許可したものだけを選択させる。
# ---------------------------------------------------------------------------

ALLOWED_WORKSPACES: dict[str, str] = {
    "local_llm_agent": "/mnt/c/Users/tamura/workspace/local_llm_agent",
}


class PathEscapeError(ValueError):
    """パスがbase_dirの外を指している場合"""


def resolve_safe_path(base_dir: str, path: str) -> str:
    """path を base_dir 配下に限定して実体パスに解決する。
    base_dir の外を指す場合(相対パストラバーサル・シンボリックリンク経由含む)は
    PathEscapeError を送出する。
    """
    base_real = os.path.realpath(base_dir)

    # 相対パスは base_dir からの相対として解釈する
    candidate = path if os.path.isabs(path) else os.path.join(base_real, path)
    target_real = os.path.realpath(candidate)

    if target_real != base_real and not target_real.startswith(base_real + os.sep):
        raise PathEscapeError(
            f"許可されたワークスペース({base_dir})の外を指すパスです: {path}"
        )
    return target_real


# ---------------------------------------------------------------------------
# list_files / read_file / write_file
# ---------------------------------------------------------------------------


def list_files(base_dir: str, directory: str) -> str:
    """指定したディレクトリのファイル一覧を返す(base_dir配下に限定)"""
    try:
        safe_path = resolve_safe_path(base_dir, directory)
    except PathEscapeError as e:
        return f"エラー: {e}"

    try:
        files = os.listdir(safe_path)
        return "\n".join(files)
    except Exception as e:
        return f"エラー: {e}"


def read_file(base_dir: str, path: str) -> str:
    """指定したファイルの中身を読んで返す(base_dir配下に限定)"""
    try:
        safe_path = resolve_safe_path(base_dir, path)
    except PathEscapeError as e:
        return f"エラー: {e}"

    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return f"エラー: ファイルが見つかりません: {path}"
    except UnicodeDecodeError:
        return f"エラー: テキストとして読めないファイルです(画像やバイナリの可能性): {path}"
    except Exception as e:
        return f"エラー: {e}"


def write_file(base_dir: str, path: str, content: str) -> str:
    """指定したファイルに内容を書き込む(新規作成 or 上書き、base_dir配下に限定)。
    呼び出し前提: confirm済み"""
    try:
        safe_path = resolve_safe_path(base_dir, path)
    except PathEscapeError as e:
        return f"エラー: {e}"

    try:
        with open(safe_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"成功: {path} に書き込みました"
    except Exception as e:
        return f"エラー: {e}"


# ---------------------------------------------------------------------------
# execute_command
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SEC = 30

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
    "pip",  # list/show 系のみ許可。install/uninstall等は書き込み判定に回る
    "npm",  # list/show 系のみ許可。install/run等は書き込み判定に回る
}

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


def is_readonly_command(command: str) -> bool:
    """コマンド文字列が読み取り専用とみなせるか判定する。"""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False

    if not tokens:
        return False

    if any(op in command for op in (">", ">>", "|", "&&", ";", "<", "`", "$(")):
        return False

    head = tokens[0]
    if head not in READONLY_COMMANDS:
        return False

    write_subs = WRITE_SUBCOMMANDS.get(head)
    if write_subs and len(tokens) > 1 and tokens[1] in write_subs:
        return False

    return True


def execute_command(
    base_dir: str, command: str, timeout: int = DEFAULT_TIMEOUT_SEC
) -> str:
    """シェルコマンドを実行する(confirm判断は呼び出し側=APIレイヤーで行う)。
    cwdをbase_dirに固定するが、絶対パス指定や `..` を使った移動までは防げない点に注意。
    """
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=base_dir,
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


# ---------------------------------------------------------------------------
# edit_file (プレビューと実行を分離)
# ---------------------------------------------------------------------------


@dataclass
class EditPreview:
    ok: bool
    message: str  # ok=Falseの場合はエラー内容、ok=Trueの場合はdiff


def preview_edit(base_dir: str, path: str, search: str, replace: str) -> EditPreview:
    """実際には書き込まず、一意性チェックとdiff生成だけ行う(base_dir配下に限定)"""
    try:
        safe_path = resolve_safe_path(base_dir, path)
    except PathEscapeError as e:
        return EditPreview(ok=False, message=str(e))

    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        return EditPreview(ok=False, message=f"ファイルが見つかりません: {path}")
    except Exception as e:
        return EditPreview(ok=False, message=str(e))

    count = original.count(search)
    if count == 0:
        return EditPreview(
            ok=False,
            message="search文字列がファイル内に見つかりませんでした。完全に一致する文字列を指定してください。",
        )
    if count > 1:
        return EditPreview(
            ok=False,
            message=f"search文字列がファイル内に{count}箇所存在し一意に特定できません。前後の文脈を含めて一意になるようsearchを広げてください。",
        )

    updated = original.replace(search, replace, 1)
    diff = "\n".join(
        difflib.unified_diff(
            original.splitlines(),
            updated.splitlines(),
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )
    return EditPreview(ok=True, message=diff)


def edit_file(base_dir: str, path: str, search: str, replace: str) -> str:
    """実際に置換を実行する(base_dir配下に限定)。
    呼び出し前提: preview_editでok=True確認済み・confirm済み"""
    try:
        safe_path = resolve_safe_path(base_dir, path)
    except PathEscapeError as e:
        return f"エラー: {e}"

    try:
        with open(safe_path, "r", encoding="utf-8") as f:
            original = f.read()
    except Exception as e:
        return f"エラー: {e}"

    count = original.count(search)
    if count != 1:
        return f"エラー: search文字列が一意に特定できません(該当箇所: {count})"

    updated = original.replace(search, replace, 1)
    try:
        with open(safe_path, "w", encoding="utf-8") as f:
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


# ---------------------------------------------------------------------------
# Ollama function calling 用のツール定義
# ---------------------------------------------------------------------------

TOOLS = [
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
            "description": "シェルコマンドを実行する。読み取り専用コマンド(ls, cat, git status等)は確認なしで実行されるが、変更を伴う可能性のあるコマンドはユーザー確認が必要。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "実行するコマンド"}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "ファイル内のテキストを厳密一致・単一置換で書き換える。search文字列はファイル内で一意でなければならない。",
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

# confirmなしで即実行してよいツール(execute_commandは動的判定のため含めない)
NO_CONFIRM_TOOLS = {"list_files", "read_file"}
# 常にconfirm必須のツール
ALWAYS_CONFIRM_TOOLS = {"write_file", "edit_file"}
