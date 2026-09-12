"""
ブラウザ(Nuxt4)から使うFastAPIバックエンド。

エンドポイント:
  GET  /api/models       利用可能なOllamaモデル一覧を返す
  GET  /api/workspaces   選択可能なワークスペース(BASE_DIR)の一覧を返す
  POST /api/chat          プロンプトを送信し、ツール呼び出しが必要なら実行して最終回答を返す。
                          確認が必要なツールの場合は status="pending_confirmation" を返す。
  POST /api/confirm       pending_id を承認/却下して会話を再開する。

状態管理:
  会話の途中経過(confirm待ちの状態)は pending_id をキーにメモリ上の辞書に保持する。
  プロセス再起動で消える点はローカル単一ユーザー用のMVPとして許容している。

ワークスペース(BASE_DIR)について:
  ブラウザからの自由入力は受け付けず、agent_tools.ALLOWED_WORKSPACES に
  事前登録された名前のみ選択できる。実際の操作範囲はサーバー側のこの辞書だけが決める。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import ollama
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import agent_tools

app = FastAPI(title="local-llm-agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SYSTEM_PROMPT = {
    "role": "system",
    "content": "あなたはファイル操作を手伝うアシスタントです。",
}

# pending_id -> {"model": str, "base_dir": str, "messages": [...], "message": ..., "tool_name": str, "args": dict}
PENDING: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# スキーマ
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    model: str
    workspace: str
    prompt: str


class ConfirmRequest(BaseModel):
    pending_id: str
    approved: bool


class ChatResponse(BaseModel):
    status: str  # "final" | "pending_confirmation"
    answer: str | None = None
    pending_id: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    preview: str | None = None


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------


def resolve_workspace(name: str) -> str:
    base_dir = agent_tools.ALLOWED_WORKSPACES.get(name)
    if base_dir is None:
        raise HTTPException(
            status_code=400, detail=f"許可されていないワークスペースです: {name}"
        )
    return base_dir


def extract_tool_calls(message) -> list | None:
    """通常のtool_callsが空の場合、content内の生JSONをフォールバックでパースする"""
    tool_calls = message.tool_calls
    if tool_calls:
        return tool_calls

    content = (message.content or "").strip()
    if not content:
        return None
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


def run_tool(base_dir: str, name: str, args: dict) -> str:
    """confirm不要と判定されたツール、またはconfirm済みのツールを実際に実行する"""
    if name == "list_files":
        return agent_tools.list_files(base_dir, args.get("directory", "."))
    if name == "read_file":
        return agent_tools.read_file(base_dir, args.get("path", ""))
    if name == "write_file":
        return agent_tools.write_file(
            base_dir, args.get("path", ""), args.get("content", "")
        )
    if name == "execute_command":
        return agent_tools.execute_command(base_dir, args.get("command", ""))
    if name == "edit_file":
        return agent_tools.edit_file(
            base_dir,
            args.get("path", ""),
            args.get("search", ""),
            args.get("replace", ""),
        )
    return f"エラー: 未対応の道具です: {name}"


def build_preview(base_dir: str, name: str, args: dict) -> tuple[bool, str]:
    """confirm待ちにする際、フロントエンドに見せるプレビューを作る。
    戻り値: (ok, preview_text)。ok=False の場合はそもそもconfirm不要でエラー即返却。
    """
    if name == "write_file":
        return (
            True,
            f"{args.get('path')} に以下の内容を書き込みます:\n\n{args.get('content', '')}",
        )
    if name == "execute_command":
        return (
            True,
            f"次のコマンドを実行します(作業ディレクトリ: {base_dir}):\n  {args.get('command')}",
        )
    if name == "edit_file":
        result = agent_tools.preview_edit(
            base_dir,
            args.get("path", ""),
            args.get("search", ""),
            args.get("replace", ""),
        )
        if not result.ok:
            return False, result.message
        return True, result.message
    return True, ""


def needs_confirmation(name: str, args: dict) -> bool:
    if name in agent_tools.NO_CONFIRM_TOOLS:
        return False
    if name == "execute_command":
        return not agent_tools.is_readonly_command(args.get("command", ""))
    return name in agent_tools.ALWAYS_CONFIRM_TOOLS


def continue_chat(model: str, messages: list) -> str:
    """ツール実行結果を踏まえてモデルに最終回答を生成させる"""
    response = ollama.chat(model=model, messages=messages)
    return response["message"]["content"]


# ---------------------------------------------------------------------------
# エンドポイント
# ---------------------------------------------------------------------------


@app.get("/api/models")
def get_models():
    """インストール済みのOllamaモデル一覧を返す"""
    try:
        result = ollama.list()
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"Ollamaへの接続に失敗しました: {e}"
        )
    return {"models": [m["model"] for m in result.get("models", [])]}


@app.get("/api/workspaces")
def get_workspaces():
    """選択可能なワークスペース名の一覧を返す(実際のパスは返さない)"""
    return {"workspaces": list(agent_tools.ALLOWED_WORKSPACES.keys())}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    base_dir = resolve_workspace(req.workspace)
    messages = [SYSTEM_PROMPT, {"role": "user", "content": req.prompt}]

    response = ollama.chat(model=req.model, messages=messages, tools=agent_tools.TOOLS)
    message = response["message"]
    tool_calls = extract_tool_calls(message)

    if not tool_calls:
        return ChatResponse(status="final", answer=message.content or "")

    # MVPとして最初の1件のみ処理する(複数ツール呼び出しの逐次処理は未対応)
    tool_call = tool_calls[0]
    name = tool_call.function.name
    args = tool_call.function.arguments

    if needs_confirmation(name, args):
        ok, preview = build_preview(base_dir, name, args)
        if not ok:
            # プレビュー生成自体がエラー(例: edit_fileのsearchが一意でない、パスがbase_dir外)
            # → confirm不要でエラーとして完結させ、モデルに続きを考えさせる
            messages.append(message)
            messages.append({"role": "tool", "content": preview, "tool_name": name})
            answer = continue_chat(req.model, messages)
            return ChatResponse(status="final", answer=answer)

        pending_id = str(uuid.uuid4())
        PENDING[pending_id] = {
            "model": req.model,
            "base_dir": base_dir,
            "messages": messages,
            "message": message,
            "tool_name": name,
            "args": args,
        }
        return ChatResponse(
            status="pending_confirmation",
            pending_id=pending_id,
            tool_name=name,
            tool_args=args,
            preview=preview,
        )

    # confirm不要 → 即実行して最終回答まで進める
    result = run_tool(base_dir, name, args)
    messages.append(message)
    messages.append({"role": "tool", "content": result, "tool_name": name})
    answer = continue_chat(req.model, messages)
    return ChatResponse(status="final", answer=answer)


@app.post("/api/confirm", response_model=ChatResponse)
def confirm(req: ConfirmRequest):
    pending = PENDING.pop(req.pending_id, None)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail="該当するpending_idが見つかりません(サーバー再起動やタイムアウトの可能性)",
        )

    messages = pending["messages"]
    message = pending["message"]
    name = pending["tool_name"]
    args = pending["args"]
    base_dir = pending["base_dir"]

    if req.approved:
        result = run_tool(base_dir, name, args)
    else:
        result = "ユーザーが実行をキャンセルしました"

    messages.append(message)
    messages.append({"role": "tool", "content": result, "tool_name": name})

    answer = continue_chat(pending["model"], messages)
    return ChatResponse(status="final", answer=answer)
