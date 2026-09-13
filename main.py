"""
ブラウザ(Nuxt4)から使うFastAPIバックエンド。

エンドポイント:
  GET    /api/models              利用可能なOllamaモデル一覧を返す
  GET    /api/workspaces          選択可能なワークスペース(BASE_DIR)の一覧を返す
  POST   /api/chat                プロンプトを送信し、ツール呼び出しが必要なら実行して最終回答を返す。
                                   確認が必要なツールの場合は status="pending_confirmation" を返す。
  POST   /api/confirm             pending_id を承認/却下して会話を再開する。
  GET    /api/sessions            過去の会話一覧(タイトル・更新日時)を返す。
  GET    /api/sessions/{id}/messages  指定した会話の表示用ログ(user/assistantの発言のみ)を返す。
  DELETE /api/sessions/{id}       指定した会話を削除する。

状態管理:
  - 会話は session_id をキーに SESSIONS に SessionRecord として保持する。
    フロントエンドは最初のレスポンスで受け取った session_id を以降のリクエストに
    含めることで、同じ会話の続きとして扱われる。省略すると新しい会話として扱う。
    過去の会話に戻りたい場合は GET /api/sessions で一覧を取得し、
    選んだ session_id を次の /api/chat リクエストに含めればよい。
  - confirm待ちの状態は pending_id をキーに PENDING に保持する。
    session_id・base_dir・model・ツール名/引数だけを持たせれば十分で、
    会話履歴自体は SESSIONS 側のリストをそのまま書き換える(同じlistオブジェクトを
    参照しているため、confirm時に読み直す必要はない)。
  - どちらもプロセス再起動で消える点は、ローカル単一ユーザー用のMVPとして許容している。
    会話を無期限に保持し続けるとメモリを消費し続けるため、不要になった会話は
    DELETE /api/sessions/{id} で明示的に削除する運用を想定している。

ワークスペース(BASE_DIR)について:
  ブラウザからの自由入力は受け付けず、agent_tools.ALLOWED_WORKSPACES に
  事前登録された名前のみ選択できる。実際の操作範囲はサーバー側のこの辞書だけが決める。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

TITLE_MAX_LENGTH = 30


@dataclass
class SessionRecord:
    """1つの会話セッション。messagesはollamaに渡す生のメッセージ履歴
    (system/user/assistant/toolの混在。assistantのtool呼び出し部分は
    ollama側のMessageオブジェクトのまま格納されることもある)。
    """

    messages: list[Any] = field(default_factory=lambda: [dict(SYSTEM_PROMPT)])
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    updated_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# session_id -> SessionRecord
SESSIONS: dict[str, SessionRecord] = {}

# pending_id -> {"model": str, "base_dir": str, "session_id": str, "tool_name": str, "args": dict}
PENDING: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# スキーマ
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    model: str
    workspace: str
    prompt: str
    session_id: str | None = (
        None  # 省略時、または存在しないIDの場合は新しい会話として扱う
    )


class ConfirmRequest(BaseModel):
    pending_id: str
    approved: bool


class ChatResponse(BaseModel):
    status: str  # "final" | "pending_confirmation"
    session_id: str | None = None
    answer: str | None = None
    pending_id: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    preview: str | None = None


class SessionSummary(BaseModel):
    session_id: str
    title: str
    updated_at: str  # ISO8601文字列


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class DisplayMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class SessionMessagesResponse(BaseModel):
    messages: list[DisplayMessage]


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------


def resolve_workspace(name: str) -> str:
    base_dir = agent_tools.ALLOWED_WORKSPACES.get(name)
    if base_dir is None:
        raise HTTPException(
            status_code=400,
            detail=f"許可されていないワークスペースです: {name}",
        )
    return base_dir


def get_or_create_session(session_id: str | None) -> tuple[str, SessionRecord]:
    """session_idが有効なら既存の会話を返し、無ければ新規に作る"""
    if session_id and session_id in SESSIONS:
        return session_id, SESSIONS[session_id]

    new_id = str(uuid.uuid4())
    record = SessionRecord()
    SESSIONS[new_id] = record
    return new_id, record


def message_field(message: Any, field_name: str) -> Any:
    """messagesの要素はdictの場合とollamaのMessageオブジェクトの場合が混在するため、
    どちらでも同じように属性を取り出すためのヘルパー。
    """
    if isinstance(message, dict):
        return message.get(field_name)
    return getattr(message, field_name, None)


def build_title(record: SessionRecord) -> str:
    """会話一覧に表示するタイトルを、最初のユーザー発言から生成する"""
    for m in record.messages:
        if message_field(m, "role") == "user":
            content = message_field(m, "content") or ""
            content = content.strip().replace("\n", " ")
            if len(content) > TITLE_MAX_LENGTH:
                return content[:TITLE_MAX_LENGTH] + "…"
            return content or "(無題の会話)"
    return "(無題の会話)"


def build_display_messages(record: SessionRecord) -> list[DisplayMessage]:
    """user/assistantの発言のみを表示用に抽出する。
    tool呼び出しの意思表示(content空のassistantメッセージ)やtool結果、systemは除外する。
    """
    display = []
    for m in record.messages:
        role = message_field(m, "role")
        content = message_field(m, "content")
        if role in ("user", "assistant") and content:
            display.append(DisplayMessage(role=role, content=content))
    return display


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


@app.get("/api/sessions", response_model=SessionListResponse)
def list_sessions():
    """過去の会話一覧を、更新が新しい順に返す"""
    summaries = [
        SessionSummary(
            session_id=session_id,
            title=build_title(record),
            updated_at=record.updated_at.isoformat(),
        )
        for session_id, record in SESSIONS.items()
    ]
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return SessionListResponse(sessions=summaries)


@app.get(
    "/api/sessions/{session_id}/messages",
    response_model=SessionMessagesResponse,
)
def get_session_messages(session_id: str):
    """指定した会話の表示用ログ(user/assistantの発言のみ)を返す"""
    record = SESSIONS.get(session_id)
    if record is None:
        raise HTTPException(
            status_code=404, detail="該当する会話が見つかりません"
        )
    return SessionMessagesResponse(messages=build_display_messages(record))


@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    """指定した会話を削除する"""
    if session_id not in SESSIONS:
        raise HTTPException(
            status_code=404, detail="該当する会話が見つかりません"
        )
    del SESSIONS[session_id]
    return {"deleted": session_id}


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    base_dir = resolve_workspace(req.workspace)
    session_id, record = get_or_create_session(req.session_id)
    messages = record.messages
    messages.append({"role": "user", "content": req.prompt})
    record.updated_at = datetime.now(timezone.utc)

    response = ollama.chat(
        model=req.model, messages=messages, tools=agent_tools.TOOLS
    )
    message = response["message"]
    tool_calls = extract_tool_calls(message)

    if not tool_calls:
        messages.append(
            {"role": "assistant", "content": message.content or ""}
        )
        return ChatResponse(
            status="final", session_id=session_id, answer=message.content or ""
        )

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
            messages.append(
                {"role": "tool", "content": preview, "tool_name": name}
            )
            answer = continue_chat(req.model, messages)
            messages.append({"role": "assistant", "content": answer})
            return ChatResponse(
                status="final", session_id=session_id, answer=answer
            )

        # 確認待ちの間も、モデルの意思表示(tool_call)自体は会話履歴に含めておく
        messages.append(message)

        pending_id = str(uuid.uuid4())
        PENDING[pending_id] = {
            "model": req.model,
            "base_dir": base_dir,
            "session_id": session_id,
            "tool_name": name,
            "args": args,
        }
        return ChatResponse(
            status="pending_confirmation",
            session_id=session_id,
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
    messages.append({"role": "assistant", "content": answer})
    return ChatResponse(status="final", session_id=session_id, answer=answer)


@app.post("/api/confirm", response_model=ChatResponse)
def confirm(req: ConfirmRequest):
    pending = PENDING.pop(req.pending_id, None)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail="該当するpending_idが見つかりません(サーバー再起動やタイムアウトの可能性)",
        )

    session_id = pending["session_id"]
    record = SESSIONS.get(session_id)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="対応する会話セッションが見つかりません(サーバー再起動の可能性)",
        )

    messages = record.messages
    name = pending["tool_name"]
    args = pending["args"]
    base_dir = pending["base_dir"]

    if req.approved:
        result = run_tool(base_dir, name, args)
    else:
        result = "ユーザーが実行をキャンセルしました"

    messages.append({"role": "tool", "content": result, "tool_name": name})
    answer = continue_chat(pending["model"], messages)
    messages.append({"role": "assistant", "content": answer})
    record.updated_at = datetime.now(timezone.utc)
    return ChatResponse(status="final", session_id=session_id, answer=answer)
