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

測定時間(下り・上りそれぞれ)。例: 300、10m(10分)、1h(1時間)
測定時間 [10m]: 30m

測定方向: both=下り→上りの順に両方 / down=下りのみ / up=上りのみ
方向 [both]:
```

これで測定が始まり、画面に1秒ごとの速度が表示されます。
測定を途中でやめたいときは `Ctrl + C` を押してください
(それまでのデータはちゃんと保存されます)。

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
| `--collect-only` | — | 速度測定なしでアンテナ情報だけ記録 |

## 6. 測定結果の見方

測定が終わると `results/<日時>_<ラベル>/` フォルダに保存されます:

| ファイル | 内容 |
|---|---|
| `throughput.csv` | 1秒ごとの下り/上り速度(Mbps)と応答時間(ms) |
| `starlink_status.csv` | Starlinkアンテナの1秒ごとの状態(SNR、遮蔽率、衛星との通信品質など) |
| `kymeta_status.csv` | Kymetaアンテナの1秒ごとの状態(CNR、ビーム方向、GPS位置など) |
| `summary.json` | 平均・最大・最小などのまとめ |

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
| 速度が明らかに低い | PCが衛星回線「経由」でネットに出ているか確認(社内LANやテザリング経由になっていないか)。PCのWi-Fiではなく有線接続推奨 |
| Starlinkの情報が取れない | Starlink のLAN内から実行しているか確認。`192.168.100.1` にブラウザでアクセスできるかも確認 |
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
