"""
アプリケーション設定を一元管理するモジュール。

.env に定義した値は必ずここ経由で読み出し、他のモジュールでは
os.getenv() を直接呼ばず settings オブジェクトを参照する。

新しい設定を追加する場合:
  - 値が1つに決まる設定(タイムアウト秒数、CORS許可オリジン、環境名など)
    → Settings クラスにフィールドを1行追記するだけでよい
  - WORKSPACE_<名前> のようにキー名が可変で数も増減する設定
    → allowed_workspaces のような専用プロパティを用意し、環境変数を走査して構築する
      (pydanticの固定フィールドでは表現しづらいため)

開発/本番などで値を差し替えたい場合は、Settingsのデフォルト値を .env 側の値で上書きする形になる。
本番用の値は .env.production のような別ファイルに分け、デプロイ時に
`Settings(_env_file=".env.production")` のように読み込むファイルを切り替える運用を想定している。
"""

from __future__ import annotations

import os
from functools import cached_property

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# WORKSPACE_* のような「pydanticの固定フィールドにしにくい」変数も
# os.environ から読めるようにするため、python-dotenv で明示的に読み込んでおく。
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 実行環境の切り替え(development / production など)
    environment: str = "development"

    # 今後、公開したくない値や開発/本番で差分のある値をここに追記していく。
    # 例:
    # cors_origin: str = "http://localhost:3000"
    # default_command_timeout_sec: int = 30

    @cached_property
    def allowed_workspaces(self) -> dict[str, str]:
        """WORKSPACE_<名前>=<パス> という命名規則の環境変数から動的に構築する。
        ワークスペースは名前も数も可変なため、固定フィールドではなくここで走査する。
        環境変数名自体は慣習に合わせて全て大文字(例: WORKSPACE_LOCAL_LLM_AGENT)を想定し、
        ブラウザ表示用の名前としては読みやすいよう小文字に正規化する。
        """
        prefix = "WORKSPACE_"
        return {
            key[len(prefix) :].lower(): os.path.expanduser(value)
            for key, value in os.environ.items()
            if key.startswith(prefix) and value
        }


settings = Settings()
