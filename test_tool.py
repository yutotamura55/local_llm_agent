import ollama
import os


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
            return f.read()
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


system_prompt = {
    "role": "system",
    "content": "あなたはファイル操作を手伝うアシスタントです。",  # 汎用的な内容に戻す
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
]


response = ollama.chat(
    model="qwen2.5:7b",
    messages=[
        system_prompt,
        {
            "role": "user",
            "content": "「こんにちは、これはテストです」という内容のファイルを作って",
        },
    ],
    tools=tools,
)

print(response["message"])

message = response["message"]

if message.tool_calls:
    for tool_call in message.tool_calls:
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
        else:
            result = f"エラー: 未対応の道具です: {name}"

        print(f"--- {name} 実行結果 ---")
        print(result)

        follow_up = ollama.chat(
            model="qwen2.5:7b",
            messages=[
                system_prompt,
                {
                    "role": "user",
                    "content": "「こんにちは、これはテストです」という内容のファイルを作って",
                },
                message,
                {"role": "tool", "content": result, "tool_name": name},
            ],
        )
        print("--- 最終回答 ---")
        print(follow_up["message"]["content"])
