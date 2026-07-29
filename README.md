# leo_analyzer — LEO衛星回線 スループット測定ツール

Starlink / OneWeb などのLEO衛星回線の**実効スループット(下り・上り)を
1秒ごとに測定**し、同時に**アンテナの状態情報も1秒ごとにCSV記録**する
ツールです。測定結果はすべてCSVファイルで保存されるので、Excelでそのまま
開いてグラフ化できます。

---

## 1. 必要なもの

- Windows / Mac / Linux のPC(ノートPCでOK)
- そのPCが**測定したい衛星回線経由でインターネットに接続されている**こと
- Python 3.9以上(インストール方法は下記)

## 2. インストール手順

### 2-1. Pythonを入れる(未インストールの場合)

**Windows:**

1. https://www.python.org/downloads/ を開き「Download Python 3.x.x」をクリック
2. ダウンロードしたインストーラーを実行
3. **最初の画面で必ず「Add python.exe to PATH」にチェック**を入れてから
   「Install Now」をクリック
4. 確認: スタートメニューから「コマンドプロンプト」を開き、
   `python --version` と入力してEnter。`Python 3.11.x` のように表示されればOK

**Mac:**

ターミナルを開いて `python3 --version` と入力。バージョンが表示されればOK。
表示されない場合は https://www.python.org/downloads/ からインストール。

### 2-2. このツールを入手する

**Gitを使わない場合(簡単):**

1. GitHubのリポジトリページで緑色の「Code」ボタン →「Download ZIP」
2. ZIPを展開し、`leo_analyzer` フォルダを分かりやすい場所
   (例: デスクトップ)に置く

**Gitを使う場合:**

```bash
git clone <このリポジトリのURL>
```

### 2-3. 必要なライブラリを入れる

コマンドプロンプト(Macはターミナル)で `leo_analyzer` フォルダに移動して
1行実行するだけです:

```bash
cd Desktop\leo_analyzer        ← 置いた場所に合わせて変更(Macは cd Desktop/leo_analyzer)
pip install -r requirements.txt
```

`pip` が見つからないと言われた場合は `python -m pip install -r requirements.txt`
を試してください。

これでインストールは完了です。

---

## 3. 使い方(いちばん簡単な方法)

**Windowsの場合: フォルダ内の `measure.bat` をダブルクリック**するだけで
起動します。あとは画面の質問に答えるだけです。

コマンドで起動する場合も、引数なしで実行すると同じ対話モードになります:

```bash
python -m leo_analyzer
```

```
============================================================
 LEO回線 スループット測定ツール
============================================================
そのままEnterを押すと [ ] 内の既定値が使われます。

測定する回線を選んでください:
  1) Starlink        (アンテナ情報も同時記録)
  2) OneWeb Kymeta   (アンテナ情報も同時記録)
  3) OneWeb Intellian / その他 (速度測定のみ)
番号を入力 [3]: 1

測定内容を選んでください:
  1) 最大スループット + RTT  (回線を飽和させます。通信量大)
  2) RTTのみ                 (回線に負荷をかけません。長時間向き)
  3) アンテナ情報のみ        (通信なし)
番号を入力 [1]: 1

測定時間(下り・上りそれぞれ)。例: 300、10m(10分)、1h(1時間)
測定時間 [10m]: 30m

測定方向: both=下り→上りの順に両方 / down=下りのみ / up=上りのみ
方向 [both]:
```

これで測定が始まり、画面に1秒ごとの速度が表示されます。
測定を途中でやめたいときは `Ctrl + C` を押してください
(それまでのデータはちゃんと保存されます)。

## 3-1. 測定内容の選択(重要)

起動時に3つのモードから選べます。**通信量が大きく変わる**ので、
長時間測定の前に必ず確認してください。

| モード | 測定内容 | 用途 | 12時間の通信量 |
|---|---|---|---|
| **① フル測定**(`--measure full`) | 最大スループット + RTT + アンテナ情報 | 性能評価 | **数百GB〜1TB以上** |
| **② RTTのみ**(`--measure rtt`) | RTT + アンテナ情報(回線に負荷なし) | 走行テスト、長時間の品質監視 | 約50MB |
| **③ アンテナ情報のみ**(`--measure none`) | アンテナ情報のみ | 電波状況の記録 | ほぼ0 |

```bash
# ② RTTのみで12時間の走行テスト(回線を圧迫しない)
python -m leo_analyzer --label drive_test --measure rtt --duration 12h \
    --collect kymeta --kymeta-array-interval 60
```

**① フル測定は回線を飽和させ続けます。**200Mbpsで12時間なら約1TBを
消費するため、従量課金・データ上限のある回線では②を推奨します。
②でもRTT・疎通不能秒数・SINR・位置情報は1秒解像度で記録されるので、
ハンドオーバーや品質低下の検出は十分可能です。

## 4. 測定時間について

- `--duration`(対話モードの「測定時間」)は**下り・上りそれぞれの時間**です。
  `both` で30分を指定すると、合計約1時間の測定になります
- 秒数のほか `10m`(10分)、`1h`(1時間)のような指定ができます
- LEO回線は約15秒〜数分周期の衛星ハンドオーバーで速度が変動するため、
  **最低でも5〜10分、傾向をしっかり見るなら30分〜1時間以上**をおすすめします
  (既定値は5分です)

## 5. コマンドで細かく指定する場合

```bash
# Starlink: 30分ずつ下り/上り + アンテナ情報記録
python -m leo_analyzer --label starlink --duration 30m --collect starlink

# OneWeb + Kymeta: 1時間ずつ + アンテナ情報記録(設定ファイル不要)
python -m leo_analyzer --label oneweb_kymeta --duration 1h --collect kymeta

# OneWeb + Intellian: 測定のみ、下りだけ10分
python -m leo_analyzer --label oneweb_intellian --duration 10m --direction down
```

| オプション | 既定値 | 説明 |
|---|---|---|
| `--duration` | 300(5分) | 方向ごとの測定時間(`90s` / `10m` / `1h` 形式可) |
| `--baseline` | 5m(5分) | 無負荷でのRTT測定時間。測定開始前と下り→上りの間に入る(`0` で省略可) |
| `--direction` | both | `down` / `up` / `both` |
| `--streams` | 8 | 並列HTTPストリーム数(通常は変更不要) |
| `--label` | run | 出力フォルダ名に付くラベル |
| `--outdir` | results | 出力先フォルダ |
| `--collect` | なし | `starlink` / `kymeta`(繰り返し指定可) |
| `--measure` | full | 測定内容: `full`(スループット+RTT)/ `rtt`(RTTのみ)/ `none`(アンテナ情報のみ) |
| `--collect-only` | — | `--measure none` と同じ(旧オプション) |
| `--report DIR` | — | 既存の測定フォルダからグラフレポートを再生成 |
| `--kymeta-array-interval` | 5 | スペクトラム取得の間隔(秒)。長時間測定では大きく |
| `--keep-all-columns` | — | CSVを圧縮せず全項目を記録する |
| `--gzip-csv` | — | CSVをgzip圧縮して保存(`.csv.gz`、約1/10) |
| `--compare DIR...` | — | 複数の測定を同じ時刻軸で比較する `compare.html` を生成 |

### 長時間測定するPCの設定(Windows)

**画面がロックされるだけなら測定は止まりません。**測定が止まるのは
PCがスリープ/休止したときです。24時間測定の前に以下を設定してください:

1. **スリープを無効化** — 設定 → システム → 電源とバッテリー →
   「画面とスリープ」で、電源接続時の「次の時間が経過後にデバイスを
   スリープ状態にする」を **「なし」** に
2. **ノートPCは蓋を閉じても動作継続** — コントロールパネル →
   電源オプション → 「カバーを閉じたときの動作」→ 電源接続時
   **「何もしない」**
3. **必ずAC電源に接続**(バッテリー駆動時は別設定が適用されます)
4. **高速スタートアップ/自動更新の再起動に注意** — Windows Update の
   アクティブ時間を測定時間帯に設定しておくと自動再起動を避けられます
5. コマンドで一時的にスリープを抑止する方法もあります(測定用の
   コマンドプロンプトを開いたまま実行):
   ```
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```

画面ロック自体は無害ですが、気になる場合は 設定 → アカウント →
サインインオプション で「しばらく操作しなかった場合...」を「なし」に。

### 長時間測定とディスク容量

CSVは自動で圧縮されます。測定開始から約90秒間の観測で、**一度も値が
返らない項目は列ごと省略**し、**ずっと同じ値の項目は
`kymeta_status_static.json` に1回だけ記録**してCSVから外します
(実測で127列→十数列)。情報は失われず、後から固定値が変化した場合は
`notes` 列に、一部エンドポイントの失敗は `error` 列に記録されます。
全項目を残したい場合は `--keep-all-columns` を付けてください。

スペクトラム等の**大きなデータは自動判別してCSVから分離**し、
`kymeta_<名前>.jsonl.gz` に退避します(数値配列のほか、
`[[周波数, 電力], ...]` のような入れ子形式や4KBを超える応答も対象)。
CSVの1セルは400文字で頭打ちにしているため、想定外の巨大データが
毎行に埋め込まれてCSVが肥大化することはありません。

さらに `--gzip-csv` を付けるとCSV自体もgzip圧縮されます
(`throughput.csv.gz` / `kymeta_status.csv.gz`、おおよそ1/10)。
レポート生成は圧縮の有無どちらでも読み込めます。Excelで開くときだけ
展開してください。

24時間などの長時間測定では、容量の主因は**スペクトラム(`adc-data`、
1回約175KB)** です。gzip圧縮して保存しますが、それでも既定の5秒間隔
だと24時間で数GBになります。長時間測定では間隔を広げてください:

```bash
# 24時間、スペクトラムは1分ごと(容量を約1/12に)
python -m leo_analyzer --label oneweb_24h --duration 12h \
    --collect kymeta --kymeta-array-interval 60
```

目安(24時間・gzip後):

| スペクトラム間隔 | 概算容量 |
|---|---|
| 5秒(既定) | 約 3 GB |
| 30秒 | 約 500 MB |
| 60秒 | 約 250 MB |
| 取得しない(`--kymeta-array-interval 999999`) | 0 |

CSV(圧縮後)は24時間でも数十MB程度です。空きディスクが1GBを切ると
スペクトラムの記録を自動停止し(CSVは継続)、回復すると再開します。

## 6. 測定結果の見方

測定が終わると `results/<日時>_<ラベル>/` フォルダに保存されます:

| ファイル | 内容 |
|---|---|
| `report.html` | **グラフレポート(ダブルクリックでブラウザ表示)** |
| `throughput.csv` | 1秒ごとの下り/上り速度(Mbps)と応答時間(ms) |
| `starlink_status.csv` | Starlinkアンテナの1秒ごとの状態(SNR、遮蔽率、衛星との通信品質など) |
| `kymeta_status.csv` | Kymetaアンテナの1秒ごとの状態(CNR、ビーム方向、GPS位置など) |
| `kymeta_<名前>.jsonl.gz` | スペクトラム等の配列データ(全データ・gzip圧縮) |
| `kymeta_status_static.json` | 測定中ずっと同じ値だった項目(CSVから省いた分) |
| `kymeta_ws_<名前>.jsonl` | WebSocketストリームの全受信メッセージ |
| `summary.json` | 平均・最大・最小などのまとめ(`tool_version` にツールのバージョンを記録) |

バージョンによって記録内容・形式が変わっています。どのバージョンで
何が変わったかは [CHANGELOG.md](CHANGELOG.md) を参照してください。

### グラフレポート(report.html)

測定が終わると自動で `report.html` が生成されます。**ダブルクリックする
だけでブラウザで開けます**(ネット接続不要・1ファイル完結)。内容:

- 平均/最大速度・アイドルRTT・疎通不能秒数のサマリータイル
- スループットの時系列グラフ(下り/上り、無負荷区間は網掛け表示)
- 応答時間(RTT)の時系列グラフ(疎通が取れなかった秒は線が途切れる)
- Starlink / Kymeta のアンテナ情報グラフ(変動している項目を自動選択)
- グラフにマウスを乗せるとその秒の値がポップアップ表示されます

過去の測定フォルダから作り直すこともできます:

```bash
python -m leo_analyzer --report results/20260724_090000_starlink
```

### アンテナ比較レポート(compare.html)

StarlinkとKymetaなど**複数の測定を同じ時刻軸で重ねて比較**できます:

```bash
python -m leo_analyzer --compare results/20260727_0900_starlink results/20260727_0900_kymeta
```

`compare.html` が出力され、両者で共通して取得できる項目が
1枚のグラフに重ねて表示されます:

| 比較項目 | Starlink側の元データ | Kymeta側の元データ |
|---|---|---|
| 下り/上りスループット | `downlink/uplink_throughput_bps` | 測定値(throughput.csv) |
| 応答時間(RTT) | `pop_ping_latency_ms` | 測定値 |
| パケットロス率 | `pop_ping_drop_rate` | — |
| 信号品質 | (SNRフラグ) | `tracking-metrics` の SINR |
| 仰角 / 方位角 | `boresight_elevation/azimuth_deg` | Look Angle |
| 遮蔽 | `fraction_obstructed` | — |

時刻はUTCの実時刻で揃えるため、**別々に始めた測定でも同じ瞬間の値を
縦に見比べられます**。片方にしかない項目はその系列だけが描画されます。

単一測定の `report.html` にも、同じ仕組みで「測定値 vs アンテナ内部値」
(例: 実測RTT と アンテナが報告するPOP応答時間)を重ねた比較セクションが
入ります。

CSVはExcelでそのまま開けます。すべてのCSVに共通の時刻列
(`timestamp_utc`=世界標準時、`epoch`=通し秒)があるので、速度と
アンテナ状態を時刻で突き合わせてグラフにできます。

`throughput.csv` の例:

```
timestamp_utc,epoch,phase,mbps_down,mbps_up,latency_ms
2026-07-23T07:29:42.000Z,1784791782.001,baseline,0.0,0.0,58.2
2026-07-23T07:29:57.000Z,1784791797.001,download,182.45,0.0,163.7
```

- `phase` … `baseline`(無負荷でRTTだけ測定中)/ `download`(下り測定中)/
  `upload`(上り測定中)
- `latency_ms` … その時点の応答時間(ms)

### アイドルRTTと負荷中RTTの見方(結果の妥当性チェック)

測定は「**無負荷5分 → 下り → 無負荷5分 → 上り**」の順に進み(時間は
`--baseline` で変更可)、無負荷区間のRTTが **アイドルRTT(回線の素の
応答時間)** として記録されます。LEOはハンドオーバーの度に経路が
変わるため、アイドルRTTは数分間測って平均を取る設計にしています。

- **ハンドオーバー等で疎通が取れなかった秒は平均・最大・最小から自動で
  除外**され、その秒数は `unreachable_s` として別に集計されます
  (CSV上は `latency_ms` が空欄の行)
- 転送中のRTTは、回線を飽和させたときにバッファへ溜まる待ち時間を含む
  ため**数百ms〜に跳ね上がるのが正常**です(バッファブロート)。異常では
  なく「飽和時にはこれだけ遅延する」という回線特性です

summary.json には最小・最大・平均などが分けて集計されます:

```json
"latency_ms": {
  "idle": {                  ← アイドルRTT(この値が回線の実力)
    "samples": 292,          ← 統計に使えた秒数
    "min": 48.2,             ← 最小
    "max": 189.5,            ← 最大
    "avg": 75.1,             ← 平均(疎通不能の秒は除外済み)
    "p50": 71.0, "p90": 110.3, "p10": 55.4,
    "unreachable_s": 8       ← 疎通が取れなかった秒数(ハンドオーバー断など)
  },
  "download_loaded": { ... },  ← 下り飽和中のRTT(同じ構成)
  "upload_loaded":   { ... }   ← 上り飽和中のRTT(同じ構成)
}
```

目安: アイドルRTTの平均が Starlink で 20〜60ms、OneWeb で 50〜150ms
程度なら正常です。平均が500msを超えるような場合は経路異常(遠回りの
ゲートウェイ/PoP、VPN経由など)を疑ってください。`unreachable_s` が
多い場合はハンドオーバー断や遮蔽の影響が大きいことを示します。

## 7. アンテナ情報の記録について

### Starlink

Starlinkのルーター/アンテナのLANに接続したPCから実行してください。

うまく取得できないときは診断コマンドで原因を切り分けられます:

```bash
python -m leo_analyzer --starlink-probe
```

「プロキシ環境変数 → TCP接続 → gRPCリフレクション → get_status取得」の
4段階を順に確認し、どこで失敗しているかと対処方法を表示します
(成功時は取得できる全項目を `starlink_sample.json` に保存)。

よくある原因:

| 症状 | 原因と対処 |
|---|---|
| TCP接続で失敗 | PCがStarlinkのLANにいない。ブラウザで `http://192.168.100.1/support/statistics` が開けるか確認。市販ルーター経由やバイパスモードでは `192.168.100.0/30` への静的ルートが必要 |
| プロキシを検出 | 社内プロキシ設定があるとgRPCがLAN宛でもプロキシ経由になり失敗する(v0.7で自動的に無効化するよう修正済み) |
| `DLL load failed while importing cygrpc` | 会社支給PCなどでWindowsのアプリケーション制御ポリシーがgrpcioのDLLをブロックしている。**v0.8以降は純Python実装(h2)へ自動的に切り替わる**ので対処不要 |
| ライブラリ未導入 | `python -m pip install -r requirements.txt`(protobuf と h2 が必要) |
| gRPCリフレクションで失敗 | ファームウェアが対応していない可能性。バージョンを添えてご連絡ください |

**接続方式について**: 既定(`auto`)ではgrpcioを試し、使えない場合は
DLLを使わない純Python実装(h2ライブラリ)に自動で切り替えます。
`--starlink-transport pure` で常に純Python実装を使うこともできます
(取得できるデータは同一です)。

アンテナ(`192.168.100.1`)から毎秒、SNR・POP応答時間・パケットロス率・
遮蔽率・アンテナ向きなどを取得して記録します。特別な設定は不要です。

### OneWeb Kymeta(Hawk u8)

設定不要でそのまま動きます。アンテナの管理画面
(`https://192.168.44.2`、工場出荷のadminログイン)に自動接続し、
APIを自動探索してCNR・キャリアロック状態・ビーム方向・GPS位置などを
毎秒記録します。

KymetaのWebGUIは画面(`/#/status` など)とデータ取得APIが分離した
SPA構成のため、探索時はWebGUIアプリ本体(JS)をダウンロードして
埋め込まれたAPIパスを抽出し、一般的な候補パスと合わせて試します。
ログインもフォーム式/Basic認証の両方を自動試行します。

実機(ACU 2.6.6.62)で確認済みのエンドポイント名
(`status` / `modem` / `terminal` / `tracking-metrics`=SINR /
`point?select=RX`=ビーム指向 / `adc-data`=スペクトラム)を優先的に
探索します。

**plots / spectrum ページの数値データ**も取得対象です:

- 応答に大きな数値配列(スペクトラム、プロット履歴など)を含む
  エンドポイントは自動判別され、**全データが `kymeta_<名前>.jsonl` に
  完全保存**されます。CSV側にはその時点の要約
  (`<名前>.points / .min / .max / .avg`)が入ります
- スペクトラム(`adc-data`)は実機のWebGUIに「表示中は性能が低下する
  可能性がある」との警告があるため、**既定では5秒間隔**で取得します。
  `config/kymeta.yaml` の `array_interval` で変更できます(1で毎秒)。
  スループット測定への影響を避けたい場合は大きめの値を推奨
- WebGUIがWebSocketでリアルタイム配信している場合は自動で接続し、
  受信した全メッセージを `kymeta_ws_<名前>.jsonl` に記録します
  (切断時は自動再接続)。接続先はWebGUIアプリから自動抽出するほか、
  `config/kymeta.yaml` の `stream_endpoints` で明示指定もできます

事前に何が取れるか確認したいときは:

```bash
python -m leo_analyzer --kymeta-probe
```

自動で見つからない場合や、パスワードを変更している場合は
`config/kymeta.example.yaml` をコピーして `config/kymeta.yaml` を作り、
中身を書き換えて `--kymeta-config config/kymeta.yaml` を付けて実行して
ください。

### OneWeb Intellian

アンテナ情報の取得手段がないため、速度測定のみ対応です。

## 8. うまく動かないとき

| 症状 | 対処 |
|---|---|
| `python` が見つからない | Pythonインストール時に「Add python.exe to PATH」を入れ忘れた可能性。入れ直すのが早いです。Windowsでは `py -m leo_analyzer` も試してください |
| `pip` が見つからない | `python -m pip install -r requirements.txt` を実行 |
| `ModuleNotFoundError: No module named 'aiohttp'` | ライブラリ未インストール。ツールを起動すると自動インストールするか聞かれるので `Y` を入力(または `python -m pip install -r requirements.txt` を実行) |
| 速度が明らかに低い | PCが衛星回線「経由」でネットに出ているか確認(社内LANやテザリング経由になっていないか)。PCのWi-Fiではなく有線接続推奨 |
| Starlinkの情報が取れない | `python -m leo_analyzer --starlink-probe` で原因を切り分け(下記参照) |
| Kymetaの情報が取れない | `https://192.168.44.2` にブラウザでログインできるか確認。できる場合はWebGUIのHelpページ→APIタブでエンドポイントを確認し `config/kymeta.yaml` に記入 |
| 測定を途中でやめたい | `Ctrl + C`(それまでのデータは保存されます) |

## 9. 測定のしくみ(技術メモ)

衛星区間だけを単独で測ることはできないため、**十分に太い回線を持つ
対向サーバー(Cloudflare、世界中にPoPあり)に対してエンドツーエンドで
測り、ボトルネックである衛星リンクの実効スループットとみなす**方式です。

- LEOはRTT変動・ハンドオーバーがあるため単一TCPでは実力が出ません。
  並列HTTPストリーム(既定8本)の合計で回線を飽和させます
- すべてクライアントから外向きの接続なので、**グローバルIPが固定でも
  可変(CGNAT)でも動作**します
- 毎秒の転送バイト数を壁時計の秒境界で集計するため、アンテナ情報CSVと
  1秒単位でそのまま突合できます

## 10. 今後の拡張(未実装)

- TLE(CelesTrak等)からStarlink / OneWeb衛星の軌道を取得し、
  ハンドオーバータイミングを予測して測定結果と重ねる
- 自前サーバー(iperf3 / UDPレートランプ)モード
