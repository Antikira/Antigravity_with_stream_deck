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
│       ├── session_detector.py             # セッション状態解析（起動時完了除外・作業中/承認待ち/完了）
│       ├── window_focus.py                 # Win32 APIによるウィンドウ最前面化
│       ├── quota_reader.py                 # 5時間・日次・週間レートリミット集計
│       ├── session_page_manager.py         # 3x5キー配置管理＆1ボタンハブ＆コーナーナビ
│       ├── image_generator.py              # 動的SVGキー画像生成
│       ├── streamdeck_bridge.py            # Stream Deck WebSocket ＆ Webシミュレータ
│       ├── installer.py                    # Stream Deckプラグイン導入・削除ツール
│       ├── generate_icons.py               # 静的アイコン生成ツール
│       └── simulator_ui.html               # Web版キーボードシミュレータ
├── sdplugin/                                # Stream Deck公式プラグインパッケージ
│   └── com.user.antigravity.sdPlugin/
│       ├── manifest.json
│       ├── run.bat
│       └── Images/
├── tests/                                   # 自動テストスイート
│   ├── test_slot_copier.py
│   └── test_antigravity_monitor.py
├── data/                                    # バックアップやテンポラリデータ格納ディレクトリ
├── templates/                               # 共通マステンプレート定義
├── pyproject.toml
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
     - 5時間枠、週間（7日間）枠の消費量・枠消費率（%）を可視化。

### 導入と利用手順

#### 1. Stream Deck へのプラグインインストール
```bash
uv run python -m src.antigravity_monitor.installer
```
※ インストール後、Elgato Stream Deck ソフトウェアを起動または再起動してください。
右側のアクション一覧に「**Antigravity Monitor**」が表示され、以下のアクションが利用可能です：
- `Antigravity Hub (1-Button)`: 1ボタン集約ハブ
- `Prev Page`: 前ページキー
- `Next Page`: 次ページキー
- `Session 1` 〜 `Session 5`: 各セッションスロット
- `5-Hour Quota` / `Weekly Quota`: レートリミット

#### 2. ブラウザ版シミュレータでの動作確認
```bash
uv run python -m src.antigravity_monitor.streamdeck_bridge --server
```
ブラウザで [http://127.0.0.1:18500/](http://127.0.0.1:18500/) を開くと、1ボタン圧縮ハブおよび 3x5 キーボードのシミュレータが表示されます。クリックすると対応する VS Code ウィンドウが最前面化されます。

---

## 品質検証とテスト

```bash
uv run ruff check .
uv run pytest
```
