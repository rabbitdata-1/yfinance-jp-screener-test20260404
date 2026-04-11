"""
日本株式財務データ収集スクリプト（全項目取得・結合版・テクニカル指標追加版）
【最適化版】2回あったhistory()呼び出しを1回に統合、sleep削減、prefecture高速化
"""
import yfinance as yf
import pandas as pd
import json
import time
import argparse
from datetime import datetime
import warnings
import logging
import requests
import sys
import os
import glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils import detect_market_type, format_ticker_for_market

# ============================
# 英語カラム定義（BQ用）
# ============================
COLUMN_NAME_MAP = {
    # --- 基本情報 ---
    "会社名": "company_name",
    "銘柄コード": "ticker_code",
    "業種": "industry",
    "優先市場": "market",
    "市場タイプ": "market_type",
    "決算月": "fiscal_period",
    "都道府県": "prefecture",
    # --- 価格・バリュエーション ---
    "株価": "stock_price",
    "時価総額": "market_cap",
    "PBR": "pbr",
    "PER(会予)": "per_forward",
    "PER(過去12ヶ月)": "per_trailing",
    "PER(前年度)": "per_previous",
    "配当方向性": "payout_ratio",
    "配当利回り": "dividend_yield",
    "EPS(過去12ヶ月)": "eps_trailing",
    "EPS(予想)": "eps_forward",
    "EPS(前年度)": "eps_previous",
    # --- 財務データ ---
    "売上高": "revenue",
    "営業利益": "operating_income",
    "営業利益率": "operating_margin",
    "当期純利益": "net_income",
    "純利益率": "profit_margin",
    "ROE": "roe",
    "自己資本比率": "equity_ratio",
    "負債": "total_liabilities",
    "流動負債": "current_liabilities",
    "流動資産": "current_assets",
    "総負債": "total_debt",
    "現金及び現金同等物": "cash_and_equivalents",
    "投資有価証券": "investment_securities",
    "ネットキャッシュ": "net_cash",
    "ネットキャッシュ比率": "net_cash_ratio",
    # --- 需給・市場データ（追加） ---
    "52週高値": "week52_high",
    "52週安値": "week52_low",
    "52週高値比率": "week52_high_ratio",
    "当日出来高": "volume_current",
    "10日平均出来高": "volume_avg_10d",
    "90日平均出来高": "volume_avg_90d",
    "出来高急増率": "volume_surge_ratio",
    "空売り比率": "short_percent_float",
    "信用倍率": "short_ratio",
    "ベータ値": "beta",
    # --- テクニカル指標（追加） ---
    "MA5": "ma5",
    "MA25": "ma25",
    "MA75": "ma75",
    "MA5乖離率": "ma5_deviation",
    "MA25乖離率": "ma25_deviation",
    "MA75乖離率": "ma75_deviation",
    "RSI14": "rsi14",
    "BB上限": "bb_upper",
    "BB下限": "bb_lower",
    "BBバンド幅": "bb_width",
    "BB%B": "bb_pct_b",
}

warnings.filterwarnings("ignore")
os.makedirs("Export", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("Export/stock_data_log.txt", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ============================
# 共通関数
# ============================
def format_ticker(code, market_type=None):
    code_str = str(code).strip()
    if market_type is None:
        market_type = detect_market_type(code_str)
    return format_ticker_for_market(code_str, market_type)

def safe_get(df, row, col):
    try:
        val = df.loc[row, col]
        return val if pd.notna(val) else None
    except:
        return None

def calculate_net_cash(current_assets, investments, total_liabilities):
    try:
        if current_assets is not None and total_liabilities is not None:
            inv = investments * 0.7 if investments else 0
            return current_assets + inv - total_liabilities
    except:
        return None

def get_prefecture_from_zip(zip_code):
    """
    【最適化】タイムアウトを3秒に短縮（元は10秒）
    接続失敗時はNoneを返してスキップ
    """
    try:
        if not zip_code:
            return None
        clean_zip = str(zip_code).replace("-", "").replace(" ", "")
        if len(clean_zip) < 7:
            return None
        url = f"https://digital-address.app/{clean_zip}"
        r = requests.get(url, timeout=3)  # ← 10秒→3秒に短縮
        r.raise_for_status()
        data = r.json()
        if data.get("addresses"):
            return data["addresses"][0].get("pref_name")
    except:
        return None

# ============================
# テクニカル指標計算
# ============================
def calculate_rsi(series, period=14):
    """RSI（相対力指数）を計算する"""
    try:
        delta = series.diff()
        gain = delta.clip(lower=0).rolling(window=period).mean()
        loss = -delta.clip(upper=0).rolling(window=period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        val = rsi.iloc[-1]
        return round(float(val), 2) if pd.notna(val) else None
    except:
        return None

def calculate_technicals(hist, current_price):
    """
    日足OHLCVヒストリカルデータからテクニカル指標を計算する。
    【最適化】1年分のhistデータをそのまま受け取る（history()呼び出しは外側で1回のみ）
    戻り値: dict
    """
    result = {
        "MA5": None, "MA25": None, "MA75": None,
        "MA5乖離率": None, "MA25乖離率": None, "MA75乖離率": None,
        "RSI14": None,
        "BB上限": None, "BB下限": None, "BBバンド幅": None, "BB%B": None,
        "出来高急増率": None,
    }

    if hist is None or hist.empty or current_price is None:
        return result

    close = hist["Close"]
    volume = hist["Volume"]

    try:
        # --- 移動平均 ---
        if len(close) >= 5:
            ma5 = close.rolling(5).mean().iloc[-1]
            if pd.notna(ma5):
                result["MA5"] = round(float(ma5), 2)
                result["MA5乖離率"] = round((current_price - float(ma5)) / float(ma5), 4)

        if len(close) >= 25:
            ma25 = close.rolling(25).mean().iloc[-1]
            ma25_std = close.rolling(25).std().iloc[-1]
            if pd.notna(ma25):
                result["MA25"] = round(float(ma25), 2)
                result["MA25乖離率"] = round((current_price - float(ma25)) / float(ma25), 4)

                # --- ボリンジャーバンド（25日・2σ）---
                if pd.notna(ma25_std):
                    bb_upper = float(ma25) + 2 * float(ma25_std)
                    bb_lower = float(ma25) - 2 * float(ma25_std)
                    bb_width = bb_upper - bb_lower
                    result["BB上限"] = round(bb_upper, 2)
                    result["BB下限"] = round(bb_lower, 2)
                    result["BBバンド幅"] = round(bb_width, 2)
                    if bb_width > 0:
                        result["BB%B"] = round((current_price - bb_lower) / bb_width, 4)

        if len(close) >= 75:
            ma75 = close.rolling(75).mean().iloc[-1]
            if pd.notna(ma75):
                result["MA75"] = round(float(ma75), 2)
                result["MA75乖離率"] = round((current_price - float(ma75)) / float(ma75), 4)

        # --- RSI（14日）---
        if len(close) >= 15:
            result["RSI14"] = calculate_rsi(close, 14)

        # --- 出来高急増率（直近1日 vs 90日平均）---
        if len(volume) >= 2:
            recent_vol = volume.iloc[-1]
            avg_vol_90 = volume.rolling(min(90, len(volume))).mean().iloc[-1]
            if pd.notna(recent_vol) and pd.notna(avg_vol_90) and avg_vol_90 > 0:
                result["出来高急増率"] = round(float(recent_vol) / float(avg_vol_90), 4)

    except Exception as e:
        logger.warning(f"テクニカル計算エラー: {e}")

    return result

# ============================
# メイン取得ロジック
# ============================
def get_stock_data(stock_info):
    code = stock_info["コード"]
    market_type = stock_info.get("市場タイプ") or detect_market_type(str(code))
    ticker_symbol = format_ticker(str(code), market_type)

    try:
        ticker = yf.Ticker(ticker_symbol)
        info = ticker.info
        if not info:
            return None

        financials = ticker.financials
        balance_sheet = ticker.balance_sheet

        # ============================================================
        # 【最適化】history()を1回だけ呼び出す（元は2回: 1y + 6mo）
        # 1年分を取得し、前年度PERとテクニカル指標の両方に使う
        # ============================================================
        hist_1y = None
        try:
            hist_1y = ticker.history(period="1y")
        except Exception as e:
            logger.warning(f"{ticker_symbol} history取得失敗: {e}")

        # 決算月
        settlement_period = None
        if not balance_sheet.empty:
            cols = balance_sheet.columns.tolist()
            if cols:
                settlement_period = str(cols[0]).split(" ")[0]

        # ======================
        # 前年度EPS / PER（1年分のhistから計算）
        # ======================
        previous_eps = None
        previous_per = None
        if not financials.empty and len(financials.columns) >= 2:
            prev_col = financials.columns[1]
            net_income_prev = safe_get(financials, "Net Income", prev_col)
            shares_prev = safe_get(financials, "Diluted Average Shares", prev_col)
            if (
                net_income_prev is not None
                and shares_prev is not None
                and shares_prev != 0
            ):
                previous_eps = net_income_prev / shares_prev
                # 【最適化】別途history(1y)を呼ばず、取得済みhist_1yを流用
                try:
                    if hist_1y is not None and not hist_1y.empty and previous_eps:
                        previous_price = hist_1y["Close"].iloc[0]
                        previous_per = previous_price / previous_eps
                except:
                    pass

        # ======================
        # 財務データ
        # ======================
        revenue = operating_income = net_income = None
        if not financials.empty:
            col = financials.columns[0]
            revenue = safe_get(financials, "Total Revenue", col)
            operating_income = safe_get(financials, "Operating Income", col)
            net_income = safe_get(financials, "Net Income", col)

        total_assets = total_liabilities = current_assets = None
        current_liabilities = total_debt = cash = investments = None
        equity_ratio = None
        if not balance_sheet.empty:
            col = balance_sheet.columns[0]
            total_assets = safe_get(balance_sheet, "Total Assets", col)
            total_liabilities = safe_get(balance_sheet, "Total Liabilities Net Minority Interest", col)
            current_assets = safe_get(balance_sheet, "Current Assets", col)
            current_liabilities = safe_get(balance_sheet, "Current Liabilities", col)
            total_debt = safe_get(balance_sheet, "Total Debt", col)
            cash = safe_get(balance_sheet, "Cash And Cash Equivalents", col)
            investments = safe_get(balance_sheet, "Available For Sale Securities", col)
            equity = safe_get(balance_sheet, "Stockholders Equity", col)
            if equity is not None and total_assets not in [None, 0]:
                equity_ratio = equity / total_assets

        net_cash = calculate_net_cash(current_assets, investments, total_liabilities)
        market_cap = info.get("marketCap")
        net_cash_ratio = (
            net_cash / market_cap
            if net_cash is not None and market_cap not in [None, 0]
            else None
        )

        # ======================
        # 需給・市場データ
        # ======================
        current_price = info.get("regularMarketPrice")
        week52_high = info.get("fiftyTwoWeekHigh")
        week52_low = info.get("fiftyTwoWeekLow")
        week52_high_ratio = None
        if current_price and week52_high and week52_high > 0:
            week52_high_ratio = round(current_price / week52_high, 4)

        volume_current = info.get("regularMarketVolume")
        volume_avg_10d = info.get("averageVolume10days")
        volume_avg_90d = info.get("averageVolume")

        # ======================
        # テクニカル指標
        # 【最適化】取得済みのhist_1yをそのまま渡す（2回目のAPI呼び出し不要）
        # ======================
        technicals = calculate_technicals(hist_1y, current_price)

        # ======================
        # 結果
        # ======================
        result = {
            # --- 基本情報 ---
            "会社名": stock_info["銘柄名"],
            "銘柄コード": code,
            "業種": stock_info.get("33業種区分"),
            "優先市場": stock_info.get("市場・商品区分"),
            "市場タイプ": market_type,
            "決算月": settlement_period,
            "都道府県": get_prefecture_from_zip(info.get("zip")) if market_type == "JP" else None,
            # --- 価格・バリュエーション ---
            "株価": current_price,
            "時価総額": market_cap,
            "PBR": info.get("priceToBook"),
            "PER(会予)": info.get("forwardPE"),
            "PER(過去12ヶ月)": info.get("trailingPE"),
            "PER(前年度)": previous_per,
            "配当方向性": info.get("payoutRatio"),
            "配当利回り": info.get("trailingAnnualDividendYield"),
            "EPS(過去12ヶ月)": info.get("trailingEps"),
            "EPS(予想)": info.get("forwardEps"),
            "EPS(前年度)": previous_eps,
            # --- 財務データ ---
            "売上高": revenue,
            "営業利益": operating_income,
            "営業利益率": info.get("operatingMargins"),
            "当期純利益": net_income,
            "純利益率": info.get("profitMargins"),
            "ROE": info.get("returnOnEquity"),
            "自己資本比率": equity_ratio,
            "負債": total_liabilities,
            "流動負債": current_liabilities,
            "流動資産": current_assets,
            "総負債": total_debt,
            "現金及び現金同等物": cash,
            "投資有価証券": investments,
            "ネットキャッシュ": net_cash,
            "ネットキャッシュ比率": net_cash_ratio,
            # --- 需給・市場データ ---
            "52週高値": week52_high,
            "52週安値": week52_low,
            "52週高値比率": week52_high_ratio,
            "当日出来高": volume_current,
            "10日平均出来高": volume_avg_10d,
            "90日平均出来高": volume_avg_90d,
            "出来高急増率": technicals.get("出来高急増率"),
            "空売り比率": info.get("shortPercentOfFloat"),
            "信用倍率": info.get("shortRatio"),
            "ベータ値": info.get("beta"),
            # --- テクニカル指標 ---
            "MA5": technicals.get("MA5"),
            "MA25": technicals.get("MA25"),
            "MA75": technicals.get("MA75"),
            "MA5乖離率": technicals.get("MA5乖離率"),
            "MA25乖離率": technicals.get("MA25乖離率"),
            "MA75乖離率": technicals.get("MA75乖離率"),
            "RSI14": technicals.get("RSI14"),
            "BB上限": technicals.get("BB上限"),
            "BB下限": technicals.get("BB下限"),
            "BBバンド幅": technicals.get("BBバンド幅"),
            "BB%B": technicals.get("BB%B"),
        }
        return result

    except Exception as e:
        logger.error(f"{ticker_symbol} error: {e}")
        return None

# ============================
# メイン処理
# ============================
def main(json_filename):
    with open(json_filename, "r", encoding="utf-8") as f:
        stock_list = json.load(f)

    results = []
    for i, stock in enumerate(stock_list):
        r = get_stock_data(stock)
        if r:
            results.append(r)
        # 【最適化】2秒→1秒に削減（950銘柄で16分の短縮）
        time.sleep(1)
        # 進捗ログ（100銘柄ごと）
        if (i + 1) % 100 == 0:
            logger.info(f"進捗: {i + 1}/{len(stock_list)} 銘柄処理済み")

    if not results:
        logger.error("❌ データ取得失敗")
        return None

    df = pd.DataFrame(results)
    columns_order = list(COLUMN_NAME_MAP.keys())
    df = df.reindex(columns=columns_order)
    df.rename(columns=COLUMN_NAME_MAP, inplace=True)

    ts_date = datetime.now().strftime("%Y%m%d")

    # ===== Part別保存 =====
    base_name = json_filename.replace(".json", "").replace("stocks_", "")
    part_filename = f"Export/{ts_date}_{base_name}.csv"
    df.to_csv(part_filename, index=False, encoding="utf-8-sig")
    logger.info(f"📄 Part CSV出力: {part_filename}")

    # ===== 全Part結合 =====
    pattern = f"Export/{ts_date}_*.csv"
    files = glob.glob(pattern)
    df_list = []
    for file in files:
        if "combined" not in file:
            df_list.append(pd.read_csv(file))

    if df_list:
        combined_df = pd.concat(df_list, ignore_index=True)
        combined_filename = f"Export/{ts_date}_combined.csv"
        combined_df.to_csv(combined_filename, index=False, encoding="utf-8-sig")
        logger.info(f"📊 結合CSV出力: {combined_filename}")

    return df

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", nargs="?", default="stocks_sample.json")
    args = parser.parse_args()
    logger.info(f"yfinance version: {yf.__version__}")
    main(args.json_file)
