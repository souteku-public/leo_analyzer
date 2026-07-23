# leo_analyzer

LEO 衛星回線(Starlink / OneWeb)のスループットを 1 秒解像度で測定し、
同時にアンテナのテレメトリ情報を CSV に記録するツールです。

## 測定の考え方

衛星区間(アンテナ〜衛星〜地上局)だけを単独で測ることはエンドポイントからは
できないため、**対向を十分に太い回線のサーバーにしてエンドツーエンドで測り、
ボトルネックである衛星リンクの実効スループットとみなす** 方式をとります。

- 対向は Cloudflare のスピードテスト用エンドポイント(`speed.cloudflare.com`)。
  世界中の PoP にエニーキャストされるため、LEO の地上局から近い経路になりやすく、
  回線側がボトルネックになる条件を満たしやすい
- LEO は RTT 変動・約 15 秒周期の衛星ハンドオーバーがあるため、単一 TCP では
  実力が出ない。**並列 HTTP ストリーム(既定 8 本)の合計**で飽和させる
- 毎秒の転送バイト数を集計して Mbps を記録。負荷中のレイテンシ(loaded latency)も
  毎秒 1 回の軽量プローブで記録し、ハンドオーバーによる落ち込みを観測できる
- クライアントから外向きに接続するだけなので、**グローバル IP が固定でも可変
  (CGNAT)でも動作する**

## インストール

Python 3.9 以上。

```bash
pip install -r requirements.txt
```

Starlink テレメトリ収集(`--collect starlink`)を使わない場合は
`aiohttp` と `PyYAML` だけで動作します。

## 使い方

### スループット測定のみ

```bash
python -m leo_analyzer --label starlink_mini --duration 60
```

下り 60 秒 → 上り 60 秒を測定し、`results/<UTC時刻>_starlink_mini/` に出力します。

主なオプション:

| オプション | 既定値 | 説明 |
|---|---|---|
| `--duration` | 60 | 方向ごとの測定秒数 |
| `--streams` | 8 | 並列 HTTP ストリーム数 |
| `--direction` | both | `down` / `up` / `both` |
| `--outdir` | results | 出力先ベースディレクトリ |
| `--label` | run | 出力ディレクトリ名に付くラベル |

### Starlink Mini のテレメトリを同時記録

Starlink の LAN 内から実行してください(ディッシュの gRPC
`192.168.100.1:9200` に到達できる必要があります)。

```bash
python -m leo_analyzer --label starlink_mini --duration 60 --collect starlink
```

ディッシュから毎秒取得した状態(`downlink_throughput_bps`,
`uplink_throughput_bps`, `pop_ping_latency_ms`, `pop_ping_drop_rate`,
遮蔽率、ボアサイト方位/仰角、SNRフラグ等)が `starlink_status.csv` に
全フィールド展開で記録されます。ルーターをバイパスしている場合も
`--starlink-addr` でアドレスを変更できます。

### OneWeb (Kymeta) のテレメトリを同時記録

Kymeta は WebGUI の背後にある JSON API を毎秒ポーリングします。
エンドポイントは機種・ファームで異なるため YAML で設定します。

1. `config/kymeta.example.yaml` を `config/kymeta.yaml` にコピー
2. ブラウザで WebGUI を開き、開発者ツール(F12)→ネットワークタブで
   GUI が定期取得している JSON の URL とログイン方式を確認して記入
3. 実行:

```bash
python -m leo_analyzer --label oneweb_kymeta --duration 60 \
    --collect kymeta --kymeta-config config/kymeta.yaml
```

OneWeb (Intellian) はアンテナ情報を取得できないため、スループット測定のみ
(`--collect` なし)で実行してください。

### 測定なしでテレメトリだけ記録

```bash
python -m leo_analyzer --label idle_watch --duration 300 \
    --collect starlink --collect-only
```

## 出力

`results/<UTC時刻>_<label>/` 配下:

- `throughput.csv` — 1 秒ごとの `timestamp_utc, epoch, phase, mbps_down, mbps_up, latency_ms`
- `starlink_status.csv` / `kymeta_status.csv` — 1 秒ごとのアンテナテレメトリ
  (全ファイル共通で `timestamp_utc` / `epoch` 列を持つため突合可能)
- `summary.json` — 平均 / 最大 / 最小 / p10 / p50 / p90 などの統計

CSV はすべて壁時計の秒境界に同期してサンプリングしているため、
スループットとアンテナテレメトリを epoch 列でそのまま JOIN できます。

## 今後の拡張(未実装)

- TLE(CelesTrak 等)から Starlink / OneWeb 衛星の軌道を取得し、
  ハンドオーバータイミングを予測して測定結果と重ねる
- 自前サーバー(iperf3 / UDP レートランプ)モード
