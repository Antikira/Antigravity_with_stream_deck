# Antigravity with Stream Deck Plugin

Antigravityのアクティブセッションを監視し、Stream Deckのキー上にリアルタイムで状態を可視化するプラグインです。

---

## プロジェクト構成

```
streamdeck_compornent/
├── src/                                     # ソースコード格納ディレクトリ
│   ├── profile_slot_copier/                # プロファイル・マス設定コピーツール
│   └── antigravity_monitor/                
│       ├── __init__.py
│       ├── extensibility_contracts.py      # OCP/DIP 共有プロトコル・コンテキスト・レジストリ定義
│       ├── session_detector.py             # セッション状態解析（起動時完了除外・作業中/承認待ち/完了）
│       ├── config.py                       # 設定モデル・フィルタ/ソートレジストリ管理
│       ├── collector.py                    # バックグラウンド収集ワーカー（Session/Quota ポーリング）
│       ├── state_store.py                  # スレッドセーフな状態スナップショット管理
│       ├── window_focus.py                 # Win32 APIによるウィンドウ最前面化
│       ├── quota_reader.py                 # 5時間・日次・週間レートリミット集計
│       ├── session_page_manager.py         # 3x5キー配置管理＆1ボタンハブ＆コーナーナビ
│       ├── image_generator.py              # 動的SVGキー画像生成・Hubテーマ/SVGレンダラーレジストリ
│       ├── streamdeck_bridge.py            # Stream Deck WebSocket ＆ Webシミュレータ
│       ├── installer.py                    # Stream Deckプラグイン導入・削除ツール
│       ├── generate_icons.py               # 静的アイコン生成ツール
│       └── simulator_ui.html               # Web版キーボードシミュレータ
├── docs/                                    # 設計ドキュメント
│   └── antigravity_monitor_extensibility_architecture.md  # OCP/DIP 拡張アーキテクチャ設計書
├── sdplugin/                                # Stream Deck公式プラグインパッケージ
│   └── com.user.antigravity.sdPlugin/
│       ├── manifest.json
│       ├── run.bat
│       └── Images/
├── tests/                                   # 自動テストスイート
│   ├── test_slot_copier.py
│   ├── test_antigravity_monitor.py
│   ├── test_antigravity_extensibility.py
│   ├── test_conversation_list_settings.py
│   ├── test_hub_settings_separation.py
│   └── test_streamdeck_bridge_main.py
├── data/                                    # バックアップやテンポラリデータ格納ディレクトリ
├── templates/                               # 共通マステンプレート定義
├── pyproject.toml
├── install_plugin.bat                       # ワンステップ・クリーンインストーラ (uv隔離環境)
└── readme.md                                # 本ドキュメント
```

---


## Antigravity セッション監視・操作プラグイン (`antigravity_monitor`)

- 目的: `agy` CLI および VS Code 組み込み Antigravity のアクティブセッションをリアルタイムで監視・操作する。
- 主な機能**:
  1. 起動時完了ジョブの除外:
     - システム起動時に稼働中だったセッションや、起動後に新しく開始・更新されたアクティブなセッションのみが追跡されます。
  2. *ボタン圧縮版（Antigravity Hub）:
     - 全体のセッション状態を縮約して表示します．
     - **色の優先度ルール**: `承認待ち` (赤) ＞ `作業中` (青) ＞ `完了` (緑)
     - キーを押すと、最も対応が必要なセッション（承認待ち > 作業中 > 最新完了）のウィンドウが最前面化されます。
  3. 3x5 コーナーナビゲーション（フォルダ／サブページレイアウト
     - `[ ↖ 戻る ]` (親プロファイル / フォルダへ戻る)
     - `[ ◀ 前頁 ]` (ページ戻り)
     - `[ 次頁 ▶ ]` (ページ送り)
     - 5つのセッションスロットを表示し、各スロットを押すと対応するセッションのウィンドウが最前面化されます。
  4. **レートリミット（クォータ）表示**:
     - Gemini / Claude・GPT / Argon の 5時間枠・週間（7日間）枠の残量・消費率（%）を可視化。
  5. **設定ファイルによるカスタマイズ (`data/monitor_config.json`)**:
     - `hub_done_timeout_seconds`: 完了（DONE）から待機（READY）表示に戻るまでの時間（秒数）。
       - 0以上の正の数: 指定時間経過後に待機状態へ自動遷移します（デフォルト: `300` 秒）。
       - 負の数（例: `-1`）または無効値: 既存仕様（時間経過による待機遷移は行わず、DONEを表示維持）。

### 導入と利用手順

#### 1. Stream Deck へのプラグインインストール（環境非汚染・ウィンドウレス起動）

`uv` を利用してプロジェクト直下の隔離仮想環境（`.venv`）を自動構築するため、システムやグローバルの Python 環境を汚染せずに導入できます（事前に `uv` のインストールが必要です）。

```bat
install_plugin.bat
```

または、すでに `.venv` が構築済みの環境からは以下でも実行できます：
```bash
uv run python -m src.antigravity_monitor.installer --skip-venv
```

インストール時に以下が自動実行されます：
- プロジェクト直下への隔離仮想環境（`.venv`）の構築・依存パッケージ同期（`uv sync --no-dev`）
- コンソール画面を開かずに `.venv\Scripts\pythonw.exe` を直接起動するウィンドウレスランチャー（`antigravity_launcher.exe`）の生成・配置
- `%APPDATA%\Elgato\StreamDeck\Plugins\com.user.antigravity.sdPlugin` へのプラグイン配置

アンインストールする場合：
```bat
install_plugin.bat --uninstall
```

※ インストール後、Elgato Stream Deck ソフトウェアを起動または再起動してください。
右側のアクション一覧に「**Antigravity Monitor**」が表示され、以下のアクションが利用可能です：
- `Antigravity Hub (1-Button)`: 1ボタン集約ハブ
- `Prev Page`: 前ページキー
- `Next Page`: 次ページキー
- `Session 1` 〜 `Session 5`: 各セッションスロット
- `Gemini 5-Hour Limit` / `Gemini Weekly Limit` / `Claude/GPT 5-Hour Limit` / `Claude/GPT Weekly Limit` / `Argon Weekly Limit`: レートリミット表示

#### 2. ブラウザ版シミュレータでの動作確認
```bash
uv run python -m src.antigravity_monitor.streamdeck_bridge --server
```
ブラウザで [http://127.0.0.1:18500/](http://127.0.0.1:18500/) を開くと、1ボタン圧縮ハブおよび 3x5 キーボードのシミュレータが表示されます。クリックすると対応する VS Code ウィンドウが最前面化されます。

---

### 拡張アーキテクチャ（OCP / DIP）

`src/antigravity_monitor/` は開放閉鎖の原則（OCP）および依存性逆転の原則（DIP）に基づいて設計されており、既存コアクラスを修正せずに以下の拡張ポイントから機能追加・差し替えが可能です。

- **共有プロトコルとレジストリ（`extensibility_contracts.py`）**:
  - 外部データ取得・操作の抽象化: `SessionSourceProtocol`, `QuotaSourceProtocol`, `WindowFocuserProtocol`
  - セッション解析パイプライン: `SessionStatusRuleProtocol`, `TitleResolverRuleProtocol`（`SessionDetector.register_status_rule` / `register_title_resolver`）
  - フィルタ・ソート拡張: `SessionFilterRuleProtocol`, `SessionFilterAndSortRegistry`（`DEFAULT_FILTER_SORT_REGISTRY`）
  - UI / SVG テーマとレンダラー拡張: `HubStatusTheme`, `HubStatusThemeRegistry`, `KeySvgRendererRegistry`
  - キーアクション拡張: `KeyActionHandlerProtocol`, `KeyActionRegistry`（`SessionPageManager.register_key_handler`）

詳細な依存構造図・クラス図および具体的な拡張レシピは [docs/antigravity_monitor_extensibility_architecture.md](docs/antigravity_monitor_extensibility_architecture.md) を参照してください。

---

## 品質検証とテスト

```bash
uv run ruff check .
uv run pytest
```
