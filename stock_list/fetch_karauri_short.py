"""
karauri.net 空売り比率ランキング 自動取得スクリプト
---------------------------------------------------
毎週 combined.csv と同タイミングで実行してください。

使い方:
  # 空売り比率5%以上を取得してcombined.csvにmerge
  python fetch_karauri_short.py --combined Export/20260411_combined.csv

  # 閾値を変えたい場合
  python fetch_karauri_short.py --combined Export/20260411_combined.csv --threshold 3.0

  # 取得のみ（mergeしない）
  python fetch_karauri_short.py --output_only

追加されるカラム:
  short_ratio_pct     : 空売り比率（%）
  short_ratio_rank    : ランキング順位
  short_ratio_date    : データ取得日
"""

import requests
from bs4 import BeautifulSoup
import pandas as pd
import argparse
import logging
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

URL = "https://karauri.net/ranking/hiritu/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
    "Referer": "https://karauri.net/",
}


def fetch_short_ratio(threshold: float = 5.0) -> pd.DataFrame:
    """
    karauri.netから空売り比率ランキングを取得する。
    threshold: この%以上の銘柄のみ返す（デフォルト5.0）
    """
    logger.info(f"karauri.net から空売り比率データを取得中... (閾値: {threshold}%以上)")

    try:
        res = requests.get(URL, headers=HEADERS, timeout=30)
        res.raise_for_status()
        res.encoding = res.apparent_encoding
    except requests.RequestException as e:
        raise RuntimeError(f"取得失敗: {e}")

    soup = BeautifulSoup(res.text, "html.parser")

    # テーブルを探す（複数候補を順番に試みる）
    table = None
    for selector in ["table.ranking", "table#ranking", "table.stock-table", "table"]:
        table = soup.select_one(selector)
        if table:
            logger.info(f"テーブル発見: {selector}")
            break

    if table is None:
        raise RuntimeError("テーブルが見つかりませんでした。サイト構造が変わった可能性があります。")

    # ヘッダー行を取得
    headers_row = table.find("tr")
    if not headers_row:
        raise RuntimeError("ヘッダー行が見つかりません")

    col_names = [th.get_text(strip=True) for th in headers_row.find_all(["th", "td"])]
    logger.info(f"カラム: {col_names}")

    # データ行を取得
    rows = []
    for tr in table.find_all("tr")[1:]:  # ヘッダーをスキップ
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)

    if not rows:
        raise RuntimeError("データ行が見つかりません")

    logger.info(f"取得行数: {len(rows)}件")

    # DataFrameに変換（列数に合わせて柔軟対応）
    df_raw = pd.DataFrame(rows)

    # 列の自動マッピング（銘柄コード・銘柄名・空売り比率を検出）
    ticker_col = ratio_col = name_col = rank_col = None

    for i, name in enumerate(col_names):
        if "コード" in name or "code" in name.lower():
            ticker_col = i
        elif "銘柄" in name and "コード" not in name:
            name_col = i
        elif "比率" in name or "%" in name or "ratio" in name.lower():
            ratio_col = i
        elif "順位" in name or "rank" in name.lower() or name == "#":
            rank_col = i

    # 自動検出できなかった場合のフォールバック（列数から推測）
    # 典型的な構造: [順位, コード, 銘柄名, 比率, ...]
    if ticker_col is None and len(col_names) >= 3:
        ticker_col = 1
    if name_col is None and len(col_names) >= 3:
        name_col = 2
    if ratio_col is None and len(col_names) >= 4:
        ratio_col = 3
    if rank_col is None:
        rank_col = 0

    logger.info(f"列マッピング → 順位:{rank_col} コード:{ticker_col} 銘柄名:{name_col} 比率:{ratio_col}")

    # 結果DataFrame作成
    result_rows = []
    fetch_date = datetime.now().strftime("%Y-%m-%d")

    for rank, row in enumerate(rows, 1):
        try:
            # 銘柄コード（4桁に統一）
            code_raw = str(row[ticker_col]).strip() if ticker_col is not None else ""
            code = code_raw.zfill(4) if code_raw.isdigit() else code_raw
            if not code or len(code) < 4:
                continue

            # 空売り比率
            ratio_raw = str(row[ratio_col]).strip() if ratio_col is not None else ""
            ratio_str = ratio_raw.replace("%", "").replace(",", "").strip()
            short_ratio_pct = float(ratio_str)

            # 閾値フィルタ
            if short_ratio_pct < threshold:
                continue

            result_rows.append({
                "ticker_code": code,
                "company_name_karauri": str(row[name_col]).strip() if name_col is not None else "",
                "short_ratio_pct": round(short_ratio_pct, 3),
                "short_ratio_rank": int(row[rank_col]) if rank_col is not None and str(row[rank_col]).isdigit() else rank,
                "short_ratio_date": fetch_date,
            })

        except (ValueError, IndexError):
            continue

    df = pd.DataFrame(result_rows)
    logger.info(f"✅ {threshold}%以上の銘柄: {len(df)}件")
    return df


def merge_into_combined(combined_path: str, short_df: pd.DataFrame) -> pd.DataFrame:
    """combined.csv に空売り比率データをmergeする"""
    combined = pd.read_csv(combined_path, dtype={"ticker_code": str})
    combined["ticker_code"] = combined["ticker_code"].astype(str).str.strip()
    short_df["ticker_code"] = short_df["ticker_code"].astype(str).str.strip()

    # 既存列があれば削除（再取得で上書き）
    drop_cols = [c for c in ["short_ratio_pct", "short_ratio_rank", "short_ratio_date",
                              "company_name_karauri"] if c in combined.columns]
    if drop_cols:
        combined.drop(columns=drop_cols, inplace=True)

    merged = combined.merge(
        short_df[["ticker_code", "short_ratio_pct", "short_ratio_rank", "short_ratio_date"]],
        on="ticker_code",
        how="left"
    )

    hit = merged["short_ratio_pct"].notna().sum()
    logger.info(f"📊 merge完了: {hit}銘柄にマッチ（空売り比率閾値以上）")
    return merged


def main():
    parser = argparse.ArgumentParser(description="karauri.net 空売り比率取得・merge")
    parser.add_argument("--combined", default=None,
                        help="combined.csvのパス。指定するとmergeして上書き保存")
    parser.add_argument("--threshold", type=float, default=5.0,
                        help="空売り比率の閾値（%%以上を取得、デフォルト5.0）")
    parser.add_argument("--output_only", action="store_true",
                        help="取得のみ（CSV別ファイルとして保存）")
    args = parser.parse_args()

    # 1. データ取得
    short_df = fetch_short_ratio(threshold=args.threshold)

    if short_df.empty:
        logger.warning("取得データが空です。サイト構造の変化またはネットワーク問題の可能性があります。")
        return

    # 2. 取得結果を表示
    print("\n=== 空売り比率上位銘柄 ===")
    print(short_df[["short_ratio_rank", "ticker_code", "short_ratio_pct"]].to_string(index=False))

    # 3. 別ファイルとして保存（常に）
    date_str = datetime.now().strftime("%Y%m%d")
    out_path = f"Export/{date_str}_short_ratio.csv"
    short_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    logger.info(f"📄 空売り比率CSV保存: {out_path}")

    # 4. combined.csv にmerge
    if args.combined and not args.output_only:
        merged = merge_into_combined(args.combined, short_df)
        merged.to_csv(args.combined, index=False, encoding="utf-8-sig")
        logger.info(f"✅ combined.csv 更新完了: {args.combined}")

        print("\n=== mergeサマリー ===")
        print(f"空売り比率データあり銘柄: {merged['short_ratio_pct'].notna().sum()}件")
        if merged["short_ratio_pct"].notna().sum() > 0:
            print(merged[merged["short_ratio_pct"].notna()][
                ["company_name", "ticker_code", "short_ratio_pct", "stock_price", "rsi14"]
            ].sort_values("short_ratio_pct", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    # BeautifulSoupがなければインストール
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        import subprocess, sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "beautifulsoup4"])
        from bs4 import BeautifulSoup

    main()
