"""
台股週線選股系統 - 資料抓取腳本
執行方式：python screener.py
結果輸出：data/results.json（供 index.html 讀取）
"""

import requests
import json
import time
import os
from datetime import datetime, timedelta

# ─── 設定 ─────────────────────────────────────────────────────
PARAMS = {
    "lookback_weeks": 26,      # 新高回測週數
    "corr_weeks": 4,           # 修正週數上限
    "max_corr_pct": 10.0,      # 最大修正幅度 %
    "ma_short": 10,            # 短均線（週）
    "ma_long": 30,             # 長均線（週）
    "vol_break_mult": 1.5,     # 突破週量比均量倍數
    "vol_shrink_mult": 0.8,    # 修正期縮量上限倍數
}

OUTPUT_FILE = "data/results.json"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})

# ─── 取得上市股票清單 ──────────────────────────────────────────
def get_twse_stock_list():
    print("📋 取得上市股票清單...")
    url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        stocks = []
        for item in data:
            code = item.get("公司代號", "").strip()
            name = item.get("公司簡稱", "").strip()
            industry = item.get("產業別", "").strip()
            # 只要4位數字代號（排除ETF、特別股等）
            if code.isdigit() and len(code) == 4:
                stocks.append({"code": code, "name": name, "industry": industry, "market": "TWSE"})
        print(f"  → 取得 {len(stocks)} 檔上市股票")
        return stocks
    except Exception as e:
        print(f"  ✗ 上市清單失敗：{e}")
        return []

# ─── 取得上櫃股票清單 ──────────────────────────────────────────
def get_tpex_stock_list():
    print("📋 取得上櫃股票清單...")
    url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        stocks = []
        for item in data:
            code = item.get("SecuritiesCompanyCode", "").strip()
            name = item.get("CompanyName", "").strip()
            if code.isdigit() and len(code) == 4:
                stocks.append({"code": code, "name": name, "industry": "", "market": "TPEx"})
        print(f"  → 取得 {len(stocks)} 檔上櫃股票")
        return stocks
    except Exception as e:
        print(f"  ✗ 上櫃清單失敗：{e}")
        return []

# ─── 從 TWSE 抓月份日線資料 ────────────────────────────────────
def fetch_monthly_twse(stock_code, year, month):
    date_str = f"{year}{month:02d}01"
    url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={date_str}&stockNo={stock_code}"
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("stat") != "OK":
            return []
        rows = []
        for item in data.get("data", []):
            try:
                # 欄位：日期, 成交股數, 成交金額, 開盤價, 最高價, 最低價, 收盤價, 漲跌價差, 成交筆數
                close = float(item[6].replace(",", ""))
                volume = float(item[1].replace(",", ""))
                date_parts = item[0].split("/")
                # 民國年轉西元年
                real_year = int(date_parts[0]) + 1911
                date = datetime(real_year, int(date_parts[1]), int(date_parts[2]))
                rows.append({"date": date, "close": close, "volume": volume})
            except:
                continue
        return rows
    except:
        return []

# ─── 從 TPEx 抓月份日線資料 ───────────────────────────────────
def fetch_monthly_tpex(stock_code, year, month):
    date_str = f"{year}/{month:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_trading_info/st43_result.php?l=zh-tw&d={date_str}&stkno={stock_code}&s=0,asc,0"
    try:
        r = SESSION.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        rows = []
        for item in data.get("aaData", []):
            try:
                close = float(item[6].replace(",", ""))
                volume = float(item[1].replace(",", ""))
                date_parts = item[0].split("/")
                real_year = int(date_parts[0]) + 1911
                date = datetime(real_year, int(date_parts[1]), int(date_parts[2]))
                rows.append({"date": date, "close": close, "volume": volume})
            except:
                continue
        return rows
    except:
        return []

# ─── 把日線合併成週線 ──────────────────────────────────────────
def daily_to_weekly(daily_data):
    if not daily_data:
        return []
    sorted_data = sorted(daily_data, key=lambda x: x["date"])
    weeks = {}
    for row in sorted_data:
        # 以週一為基準（isocalendar 的週）
        iso = row["date"].isocalendar()
        week_key = f"{iso[0]}-W{iso[1]:02d}"
        if week_key not in weeks:
            weeks[week_key] = {"closes": [], "volumes": [], "dates": []}
        weeks[week_key]["closes"].append(row["close"])
        weeks[week_key]["volumes"].append(row["volume"])
        weeks[week_key]["dates"].append(row["date"])

    weekly = []
    for key in sorted(weeks.keys()):
        w = weeks[key]
        weekly.append({
            "date": max(w["dates"]).strftime("%Y-%m-%d"),
            "close": w["closes"][-1],       # 週收盤 = 最後一個交易日收盤
            "volume": sum(w["volumes"]),     # 週成交量 = 加總
        })
    return weekly

# ─── 計算均線 ─────────────────────────────────────────────────
def calc_ma(values, n):
    result = []
    for i in range(len(values)):
        if i < n - 1:
            result.append(None)
        else:
            window = [v for v in values[i-n+1:i+1] if v is not None]
            result.append(sum(window) / len(window) if window else None)
    return result

# ─── 選股分析邏輯 ──────────────────────────────────────────────
def analyze(stock, weekly_data, params):
    closes = [w["close"] for w in weekly_data]
    volumes = [w["volume"] for w in weekly_data]
    n = len(closes)

    if n < params["ma_long"] + 5:
        return None

    ma_s = calc_ma(closes, params["ma_short"])
    ma_l = calc_ma(closes, params["ma_long"])

    last = n - 1

    # 均線多頭
    ma_aligned = (ma_s[last] and ma_l[last] and ma_s[last] > ma_l[last])

    # 找近 lookback 週內最高點
    window_start = max(0, n - params["lookback_weeks"])
    window_closes = closes[window_start:]
    peak_val = max(window_closes)
    peak_local = len(window_closes) - 1 - window_closes[::-1].index(peak_val)
    peak_idx = window_start + peak_local
    bars_since_peak = last - peak_idx

    # 修正幅度
    corr_pct = (peak_val - closes[last]) / peak_val * 100 if peak_val > 0 else 0

    # 是否突破前期高點
    pre_start = max(0, peak_idx - params["lookback_weeks"])
    pre_peak_closes = closes[pre_start:peak_idx]
    pre_high = max(pre_peak_closes) if pre_peak_closes else 0
    is_breakout = peak_val > pre_high

    # 修正條件
    corr_ok = (1 <= bars_since_peak <= params["corr_weeks"] + 2
               and 1.0 <= corr_pct <= params["max_corr_pct"])

    # 突破量
    break_vol = volumes[peak_idx]
    avg_vol_window = [v for v in volumes[max(0, peak_idx-8):peak_idx] if v > 0]
    avg_vol = sum(avg_vol_window) / len(avg_vol_window) if avg_vol_window else 0
    vol_break_ok = avg_vol > 0 and (break_vol / avg_vol) >= params["vol_break_mult"]
    vol_ratio = round(break_vol / avg_vol, 1) if avg_vol > 0 else 0

    # 修正縮量
    corr_vols = [v for v in volumes[peak_idx+1:n] if v > 0]
    avg_corr_vol = sum(corr_vols) / len(corr_vols) if corr_vols else 0
    vol_shrink_ok = avg_vol > 0 and avg_corr_vol > 0 and (avg_corr_vol / avg_vol) <= params["vol_shrink_mult"]

    # 股價站上長均
    price_above_ma = closes[last] > (ma_l[last] or 0)

    # 長均線向上
    ma_l_curr = ma_l[last]
    ma_l_prev = ma_l[max(0, last - 4)]
    ma_trend = (ma_l_curr and ma_l_prev and ma_l_curr > ma_l_prev)

    checks = {
        "breakout": is_breakout,
        "correction": corr_ok,
        "maAlign": ma_aligned,
        "priceAboveMA": price_above_ma,
        "maTrend": ma_trend,
        "volBreak": vol_break_ok,
        "volShrink": vol_shrink_ok,
    }

    score = 0
    if checks["breakout"]:     score += 20
    if checks["correction"]:   score += 25
    if checks["maAlign"]:      score += 20
    if checks["priceAboveMA"]: score += 10
    if checks["maTrend"]:      score += 10
    if checks["volBreak"]:     score += 10
    if checks["volShrink"]:    score += 5

    core_pass = checks["breakout"] and checks["correction"] and checks["maAlign"]

    return {
        "code": stock["code"],
        "name": stock["name"],
        "industry": stock["industry"],
        "market": stock["market"],
        "price": round(closes[last], 1),
        "score": score,
        "corePass": core_pass,
        "checks": checks,
        "barsSincePeak": bars_since_peak,
        "corrPct": round(corr_pct, 1),
        "peakVal": round(peak_val, 1),
        "volRatio": vol_ratio,
        "recentCloses": closes[-12:],
        "maShort": round(ma_s[last], 1) if ma_s[last] else None,
        "maLong": round(ma_l[last], 1) if ma_l[last] else None,
    }

# ─── 主程式 ───────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("  台股週線選股系統")
    print(f"  執行時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 50)

    # 取得股票清單
    twse_stocks = get_twse_stock_list()
    tpex_stocks = get_tpex_stock_list()
    all_stocks = twse_stocks + tpex_stocks

    # 限制數量（測試時可調小，正式掃全部）
    # all_stocks = all_stocks[:50]  # 測試用：只掃前50檔

    print(f"\n📊 共 {len(all_stocks)} 檔股票待分析\n")

    # 計算需要抓哪幾個月的資料（過去約 8 個月的日線 ≈ 35 週）
    today = datetime.now()
    months_needed = []
    for i in range(9):
        d = today - timedelta(days=30 * i)
        months_needed.append((d.year, d.month))
    months_needed.reverse()

    results = []
    passed = 0
    errors = 0

    for i, stock in enumerate(all_stocks):
        code = stock["code"]
        market = stock["market"]
        print(f"[{i+1}/{len(all_stocks)}] {code} {stock['name']}", end="  ")

        # 抓日線
        daily = []
        for year, month in months_needed:
            if market == "TWSE":
                rows = fetch_monthly_twse(code, year, month)
            else:
                rows = fetch_monthly_tpex(code, year, month)
            daily.extend(rows)
            time.sleep(0.3)  # 避免過快被擋

        if len(daily) < 30:
            print(f"✗ 資料不足（{len(daily)} 筆）")
            errors += 1
            continue

        # 合併成週線
        weekly = daily_to_weekly(daily)

        if len(weekly) < 30:
            print(f"✗ 週線不足（{len(weekly)} 週）")
            errors += 1
            continue

        # 分析
        result = analyze(stock, weekly, PARAMS)
        if result:
            results.append(result)
            if result["corePass"]:
                passed += 1
                print(f"✅ 評分 {result['score']}  修正 {result['corrPct']}%")
            else:
                print(f"   評分 {result['score']}")
        else:
            print("✗ 分析失敗")
            errors += 1

    # 依評分排序
    results.sort(key=lambda x: x["score"], reverse=True)

    # 輸出 JSON
    os.makedirs("data", exist_ok=True)
    output = {
        "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "totalScanned": len(all_stocks),
        "totalAnalyzed": len(results),
        "totalPassed": passed,
        "params": PARAMS,
        "stocks": results,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 50)
    print(f"  ✅ 完成！")
    print(f"  分析：{len(results)} 檔 / 符合核心條件：{passed} 檔")
    print(f"  結果儲存至：{OUTPUT_FILE}")
    print("=" * 50)

if __name__ == "__main__":
    main()