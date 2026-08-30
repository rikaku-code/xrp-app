#!/usr/bin/env python3
"""XRP/JPY 长期持仓监控 — 回本波段计划。"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import ta
from colorama import Fore, Style, init

# ── 交易与轮询 ──────────────────────────────────────────────
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BITBANK_TICKER_URL = "https://public.bitbank.cc/xrp_jpy/ticker"
BITBANK_CANDLE_URL = "https://public.bitbank.cc/xrp_jpy/candlestick/1day/{year}"
COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_CHART_URL = "https://api.coingecko.com/api/v3/coins/ripple/market_chart"
SYMBOL = "XRPJPY"
PAIR_LABEL = "XRP/JPY"
REQUEST_HEADERS = {"User-Agent": "xrp-monitor/1.0"}
REFRESH_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 24 * 60 * 60
QUIET_HOUR_START = 0
QUIET_HOUR_END = 8

# ── 持仓与回本目标 ────────────────────────────────────────────
DEFAULT_HOLDINGS_XRP = 1258.0
DEFAULT_AVAILABLE_JPY = 11_000.0
DEFAULT_TARGET_JPY = 1_200_000.0
DCA_FRACTION = 1 / 3
SWING_SELL_FRACTION = 0.15
SWING_SELL_FRACTION_HIGH = 0.20
SWING_PROFIT_PCT = 0.12
RECOVERY_MILESTONES = (300_000, 500_000, 800_000, 1_000_000, 1_200_000)
CYCLE_YEARS = 4
CYCLE_DAYS = CYCLE_YEARS * 365
# BTC 减半锚点（XRP 4 年周期参考）
HALVING_DATES = (
    datetime(2020, 5, 11, tzinfo=ZoneInfo("UTC")),
    datetime(2024, 4, 20, tzinfo=ZoneInfo("UTC")),
)

# ── 信号阈值 ────────────────────────────────────────────────
DAILY_RSI_PERIOD = 14
DAILY_RSI_EXTREME = 25
LOW_LOOKBACK_DAYS = 30
LOW_TOUCH_TOLERANCE = 0.01
MA_SHORT = 20
MA_LONG = 50
BB_PERIOD = 20
BB_STD = 2.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30
STOCH_PERIOD = 14
STOCH_SMOOTH = 3
STOCH_OVERSOLD = 30
STOCH_OVERBOUGHT = 70
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Tokyo")

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")


@dataclass(frozen=True)
class Portfolio:
    """当前资产：XRP 数量 + 现金 + 原始投入目标。"""

    xrp_quantity: float
    cash_jpy: float
    target_jpy: float

    def xrp_value(self, price: float) -> float:
        return self.xrp_quantity * price

    def total_assets(self, price: float) -> float:
        return self.xrp_value(price) + self.cash_jpy

    def pnl(self, price: float) -> float:
        return self.total_assets(price) - self.target_jpy

    def pnl_pct(self, price: float) -> float:
        if self.target_jpy <= 0:
            return 0.0
        return self.pnl(price) / self.target_jpy * 100

    def recovery_gap(self, price: float) -> float:
        return max(0.0, self.target_jpy - self.total_assets(price))

    def recovery_pct(self, price: float) -> float:
        if self.target_jpy <= 0:
            return 0.0
        return min(1.0, self.total_assets(price) / self.target_jpy)

    def after_buy(self, buy_jpy: float, price: float) -> "Portfolio":
        if buy_jpy <= 0 or price <= 0:
            return self
        spend = min(buy_jpy, self.cash_jpy)
        if spend <= 0:
            return self
        return Portfolio(
            xrp_quantity=self.xrp_quantity + spend / price,
            cash_jpy=self.cash_jpy - spend,
            target_jpy=self.target_jpy,
        )

    def after_sell(self, sell_qty: float, price: float) -> "Portfolio":
        if sell_qty <= 0 or price <= 0:
            return self
        qty = min(sell_qty, self.xrp_quantity)
        return Portfolio(
            xrp_quantity=self.xrp_quantity - qty,
            cash_jpy=self.cash_jpy + qty * price,
            target_jpy=self.target_jpy,
        )


@dataclass(frozen=True)
class MarketSnapshot:
    price: float
    daily_rsi: float
    low_30d: float
    high_30d: float
    ma20: float
    ma50: float
    macd_hist: float
    bb_upper: float
    bb_lower: float
    stoch_k: float
    stoch_d: float
    stoch_golden: bool
    stoch_dead: bool
    data_source: str


@dataclass(frozen=True)
class PlanStep:
    action: str
    trigger_price: float
    trigger_label: str
    amount_desc: str
    result_desc: str
    amount_jpy: float = 0.0
    amount_xrp: float = 0.0


@dataclass(frozen=True)
class CurrentAction:
    """刷新时刻应执行（或等待）的操作。"""

    action: str
    title: str
    reason: str
    buy_jpy: float
    buy_xrp: float
    sell_xrp: float
    sell_jpy: float
    next_buy_price: float | None
    next_sell_price: float | None
    trigger_price: float | None


@dataclass(frozen=True)
class CycleContext:
    """近 4 年 Bitbank 历史归纳的周期位置与规律。"""

    range_low: float
    range_high: float
    position_pct: float
    drawdown_pct: float
    phase: str
    phase_detail: str
    halving_year: float
    halving_label: str
    month: int
    month_strength: str
    month_return_pct: float
    month_history: str
    p25_price: float
    p50_price: float
    p75_price: float
    data_days: int


@dataclass(frozen=True)
class TechnicalContext:
    """技术指标 — 买卖操作的实际触发依据。"""

    rsi: float
    rsi_zone: str
    buy_triggered: bool
    buy_reason: str
    buy_strength: float
    sell_triggered: bool
    sell_reason: str
    sell_strength: float
    stoch_k: float
    stoch_d: float
    stoch_golden: bool
    stoch_dead: bool


@dataclass(frozen=True)
class RecoveryPlan:
    total_assets: float
    target_jpy: float
    recovery_gap: float
    recovery_pct: float
    next_milestone: float
    next_milestone_label: str
    dca_buy_jpy: float
    buy_steps: tuple[PlanStep, ...]
    sell_steps: tuple[PlanStep, ...]
    hold_only_price: float
    swing_cycle_profit: float
    cycle: CycleContext
    technical: TechnicalContext


@dataclass(frozen=True)
class TradeAdvice:
    action: str
    strength: str
    title: str
    reason: str
    detail: str


@dataclass(frozen=True)
class Signal:
    key: str
    title: str
    console_msg: str
    card_template: str
    card_body: str
    color: str


def _load_dotenv() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value


def reload_config() -> None:
    global FEISHU_WEBHOOK_URL, FEISHU_SECRET, APP_TIMEZONE
    _load_dotenv()
    FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
    FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")
    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Tokyo")


reload_config()


def load_portfolio(
    xrp_quantity: float | None = None,
    cash_jpy: float | None = None,
    target_jpy: float | None = None,
) -> Portfolio:
    qty = xrp_quantity if xrp_quantity is not None else float(
        os.getenv("HOLDINGS_XRP", DEFAULT_HOLDINGS_XRP)
    )
    cash = cash_jpy if cash_jpy is not None else float(
        os.getenv("AVAILABLE_JPY", DEFAULT_AVAILABLE_JPY)
    )
    target = target_jpy if target_jpy is not None else float(
        os.getenv("TARGET_JPY", DEFAULT_TARGET_JPY)
    )
    return Portfolio(
        xrp_quantity=max(0.0, qty),
        cash_jpy=max(0.0, cash),
        target_jpy=max(0.0, target),
    )


def suggest_dca_jpy(cash_jpy: float) -> float:
    if cash_jpy <= 0:
        return 0.0
    return cash_jpy * DCA_FRACTION


def next_milestone(total_assets: float) -> tuple[float, str]:
    for milestone in RECOVERY_MILESTONES:
        if total_assets < milestone:
            return milestone, fmt_jpy(milestone)
    last = RECOVERY_MILESTONES[-1]
    return last, f"{fmt_jpy(last)}（已达成）"


COOLDOWN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".alert_cooldown.json"
)


class AlertCooldown:
    def __init__(
        self,
        cooldown_seconds: int,
        persist_path: str | None = COOLDOWN_FILE,
    ) -> None:
        self.cooldown_seconds = cooldown_seconds
        self.persist_path = persist_path
        self._last_sent: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.persist_path or not os.path.isfile(self.persist_path):
            return
        try:
            with open(self.persist_path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._last_sent = {k: float(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self._last_sent = {}

    def _save(self) -> None:
        if not self.persist_path:
            return
        with open(self.persist_path, "w", encoding="utf-8") as handle:
            json.dump(self._last_sent, handle)

    def can_send(self, key: str) -> bool:
        last = self._last_sent.get(key)
        if last is None:
            return True
        return (time.time() - last) >= self.cooldown_seconds

    def mark_sent(self, key: str) -> None:
        self._last_sent[key] = time.time()
        self._save()

    def remaining_hours(self, key: str) -> float:
        last = self._last_sent.get(key)
        if last is None:
            return 0.0
        remain = self.cooldown_seconds - (time.time() - last)
        return max(0.0, remain / 3600)


def now_local() -> datetime:
    return datetime.now(ZoneInfo(APP_TIMEZONE))


def is_quiet_hours(now: datetime | None = None) -> bool:
    hour = (now or now_local()).hour
    return QUIET_HOUR_START <= hour < QUIET_HOUR_END


def quiet_hours_label() -> str:
    return f"{QUIET_HOUR_START:02d}:00–{QUIET_HOUR_END:02d}:00"


def fmt_jpy(value: float) -> str:
    return f"¥{value:,.0f}"


def fmt_man(value: float) -> str:
    return f"¥{value / 10_000:.1f}万"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def fetch_binance_klines(interval: str, limit: int) -> pd.DataFrame:
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    response = requests.get(
        BINANCE_KLINES_URL, params=params, timeout=15, headers=REQUEST_HEADERS
    )
    response.raise_for_status()
    raw = response.json()
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df


def fetch_bitbank_ticker() -> float:
    response = requests.get(BITBANK_TICKER_URL, timeout=15, headers=REQUEST_HEADERS)
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") != 1:
        raise requests.RequestException("Bitbank ticker 返回失败")
    return float(payload["data"]["last"])


def fetch_bitbank_history(years: int = CYCLE_YEARS) -> pd.DataFrame:
    current_year = now_local().year
    start_year = current_year - years + 1
    frames: list[pd.DataFrame] = []

    for year in range(start_year, current_year + 1):
        url = BITBANK_CANDLE_URL.format(year=year)
        response = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        response.raise_for_status()
        payload = response.json()
        if payload.get("success") != 1:
            continue
        candles = payload["data"]["candlestick"][0]["ohlcv"]
        df = pd.DataFrame(
            candles, columns=["open", "high", "low", "close", "volume", "timestamp"]
        )
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        frames.append(df)

    if not frames:
        raise requests.RequestException("Bitbank 历史数据为空")

    daily = pd.concat(frames, ignore_index=True)
    daily = daily.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    daily["date"] = pd.to_datetime(daily["timestamp"], unit="ms")
    cutoff = daily["date"].max() - timedelta(days=CYCLE_DAYS)
    daily = daily[daily["date"] >= cutoff].reset_index(drop=True)
    return daily


def fetch_bitbank_daily() -> pd.DataFrame:
    return fetch_bitbank_history(CYCLE_YEARS)


def fetch_coingecko_daily(days: int = 90) -> tuple[float, pd.DataFrame]:
    price_resp = requests.get(
        COINGECKO_PRICE_URL,
        params={"ids": "ripple", "vs_currencies": "jpy"},
        timeout=15,
        headers=REQUEST_HEADERS,
    )
    price_resp.raise_for_status()
    price = float(price_resp.json()["ripple"]["jpy"])

    chart_resp = requests.get(
        COINGECKO_CHART_URL,
        params={"vs_currency": "jpy", "days": days, "interval": "daily"},
        timeout=15,
        headers=REQUEST_HEADERS,
    )
    chart_resp.raise_for_status()
    chart = chart_resp.json()
    prices = chart.get("prices", [])
    if len(prices) < DAILY_RSI_PERIOD + 5:
        raise requests.RequestException("CoinGecko 历史数据不足")

    df = pd.DataFrame(prices, columns=["timestamp", "close"])
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    df["volume"] = 0.0
    df["date"] = pd.to_datetime(df["timestamp"], unit="ms")
    return price, df


def _snapshot_from_daily(price: float, daily: pd.DataFrame, source: str) -> MarketSnapshot:
    rsi_series = ta.momentum.RSIIndicator(
        close=daily["close"], window=DAILY_RSI_PERIOD
    ).rsi()
    ma20_series = daily["close"].rolling(window=MA_SHORT).mean()
    ma50_series = daily["close"].rolling(window=MA_LONG).mean()
    macd_ind = ta.trend.MACD(
        close=daily["close"],
        window_slow=MACD_SLOW,
        window_fast=MACD_FAST,
        window_sign=MACD_SIGNAL,
    )
    bb_ind = ta.volatility.BollingerBands(
        close=daily["close"], window=BB_PERIOD, window_dev=BB_STD
    )
    stoch_ind = ta.momentum.StochasticOscillator(
        high=daily["high"],
        low=daily["low"],
        close=daily["close"],
        window=STOCH_PERIOD,
        smooth_window=STOCH_SMOOTH,
    )
    stoch_k_series = stoch_ind.stoch()
    stoch_d_series = stoch_ind.stoch_signal()
    k_prev, k_now = float(stoch_k_series.iloc[-2]), float(stoch_k_series.iloc[-1])
    d_prev, d_now = float(stoch_d_series.iloc[-2]), float(stoch_d_series.iloc[-1])
    stoch_golden = k_prev <= d_prev and k_now > d_now
    stoch_dead = k_prev >= d_prev and k_now < d_now

    tail = daily.tail(LOW_LOOKBACK_DAYS)
    return MarketSnapshot(
        price=price,
        daily_rsi=float(rsi_series.iloc[-1]),
        low_30d=float(tail["low"].min()),
        high_30d=float(tail["high"].max()),
        ma20=float(ma20_series.iloc[-1]),
        ma50=float(ma50_series.iloc[-1]),
        macd_hist=float(macd_ind.macd_diff().iloc[-1]),
        bb_upper=float(bb_ind.bollinger_hband().iloc[-1]),
        bb_lower=float(bb_ind.bollinger_lband().iloc[-1]),
        stoch_k=k_now,
        stoch_d=d_now,
        stoch_golden=stoch_golden,
        stoch_dead=stoch_dead,
        data_source=source,
    )


def build_chart_data(daily: pd.DataFrame, cycle: CycleContext | None = None, limit: int = 180) -> pd.DataFrame:
    work = daily.copy().tail(limit)
    work["date"] = pd.to_datetime(work["timestamp"], unit="ms", errors="coerce")
    if work["date"].isna().all():
        work["date"] = pd.RangeIndex(len(work))
    chart = work.set_index("date")
    chart["price"] = chart["close"]
    if cycle:
        chart["p25"] = cycle.p25_price
        chart["p75"] = cycle.p75_price
        return chart[["price", "p25", "p75"]]
    return chart[["price"]]


def build_snapshot() -> tuple[MarketSnapshot, pd.DataFrame]:
    errors: list[str] = []

    try:
        price = fetch_bitbank_ticker()
        daily = fetch_bitbank_daily()
        return _snapshot_from_daily(price, daily, "Bitbank"), daily
    except requests.RequestException as exc:
        errors.append(f"Bitbank: {exc}")

    try:
        price, daily = fetch_coingecko_daily(
            days=max(LOW_LOOKBACK_DAYS + DAILY_RSI_PERIOD + 5, 90)
        )
        return _snapshot_from_daily(price, daily, "CoinGecko"), daily
    except requests.RequestException as exc:
        errors.append(f"CoinGecko: {exc}")

    try:
        intraday = fetch_binance_klines("15m", 5)
        daily = fetch_binance_klines("1d", max(LOW_LOOKBACK_DAYS + DAILY_RSI_PERIOD, 60))
        price = float(intraday["close"].iloc[-1])
        binance_daily = pd.DataFrame(
            {
                "open": daily["open"],
                "high": daily["high"],
                "low": daily["low"],
                "close": daily["close"],
                "volume": daily["volume"],
                "timestamp": daily["open_time"],
            }
        )
        binance_daily["date"] = pd.to_datetime(binance_daily["timestamp"], unit="ms")
        return _snapshot_from_daily(price, binance_daily, "Binance"), binance_daily
    except requests.RequestException as exc:
        errors.append(f"Binance: {exc}")

    raise requests.RequestException(" / ".join(errors))


def at_30d_low(snapshot: MarketSnapshot) -> bool:
    threshold = snapshot.low_30d * (1 + LOW_TOUCH_TOLERANCE)
    return snapshot.price <= threshold


def _years_since_halving(now: datetime) -> tuple[float, str]:
    utc_now = now.astimezone(ZoneInfo("UTC"))
    last = HALVING_DATES[-1]
    for halving in reversed(HALVING_DATES):
        if utc_now >= halving:
            last = halving
            break
    years = (utc_now - last).total_seconds() / (365.25 * 86400)
    if years < 1:
        label = "减半后第 1 年（偏积累）"
    elif years < 2:
        label = "减半后第 2 年（偏上涨）"
    elif years < 3:
        label = "减半后第 3 年（偏过热）"
    else:
        label = "减半后第 4 年（偏回落）"
    return years, label


def _month_seasonality(history: pd.DataFrame, month: int) -> tuple[str, float, str]:
    work = history.copy()
    work["ret"] = work["close"].pct_change()
    by_month = work.groupby(work["date"].dt.month)["ret"].mean() * 100
    if month not in by_month.index:
        return "中", 0.0, "暂无月份样本"

    avg = float(by_month[month])
    rank = by_month.rank()
    if rank[month] >= 9:
        strength = "强"
    elif rank[month] <= 4:
        strength = "弱"
    else:
        strength = "中"

    month_rows = work[work["date"].dt.month == month].copy()
    month_rows["year"] = month_rows["date"].dt.year
    yearly = month_rows.groupby("year")["close"].apply(
        lambda s: (s.iloc[-1] / s.iloc[0] - 1) * 100 if len(s) > 1 else 0.0
    )
    up = int((yearly > 0).sum())
    down = int((yearly <= 0).sum())
    history_text = f"过去 {len(yearly)} 个{month}月：{up} 涨 {down} 跌，月均 {avg:+.1f}%"
    return strength, avg, history_text


def _cycle_phase(
    position_pct: float,
    drawdown_pct: float,
    halving_year: float,
) -> tuple[str, str]:
    if position_pct <= 20:
        return "筑底区", "处于 4 年区间底部 20%，历史大周期常见买点带"
    if position_pct <= 35 and drawdown_pct >= 25:
        return "回调低吸区", f"自 4 年高点回落 {drawdown_pct:.0f}%，类似周期中段打折"
    if position_pct >= 80:
        return "周期顶部区", "接近 4 年区间顶部，历史多现分批止盈"
    if position_pct >= 65 and halving_year >= 1.8:
        return "过热区", "减半后第 2 年+ 且价位偏高，宜减磅不追涨"
    if position_pct >= 55:
        return "上涨后段", "周期中上段，持有为主、反弹减码"
    if position_pct <= 45:
        return "积累区", "周期中下段，可小仓定投、不急追涨"
    return "周期中段", "处于 4 年区间中游，按表低吸高抛"


def analyze_cycle(history: pd.DataFrame, price: float, now: datetime | None = None) -> CycleContext:
    now = now or now_local()
    if history.empty:
        raise ValueError("历史数据为空")

    work = history.copy()
    if "date" not in work.columns:
        work["date"] = pd.to_datetime(work["timestamp"], unit="ms", errors="coerce")

    lo = float(work["low"].min())
    hi = float(work["high"].max())
    span = hi - lo
    position_pct = ((price - lo) / span * 100) if span > 0 else 50.0
    drawdown_pct = ((hi - price) / hi * 100) if hi > 0 else 0.0

    p25 = float(work["close"].quantile(0.25))
    p50 = float(work["close"].quantile(0.50))
    p75 = float(work["close"].quantile(0.75))

    halving_year, halving_label = _years_since_halving(now)
    month = now.month
    month_strength, month_ret, month_history = _month_seasonality(work, month)
    phase, phase_detail = _cycle_phase(position_pct, drawdown_pct, halving_year)

    return CycleContext(
        range_low=lo,
        range_high=hi,
        position_pct=position_pct,
        drawdown_pct=drawdown_pct,
        phase=phase,
        phase_detail=phase_detail,
        halving_year=halving_year,
        halving_label=halving_label,
        month=month,
        month_strength=month_strength,
        month_return_pct=month_ret,
        month_history=month_history,
        p25_price=p25,
        p50_price=p50,
        p75_price=p75,
        data_days=len(work),
    )


def rsi_zone_label(rsi: float) -> str:
    if rsi >= RSI_OVERBOUGHT:
        return "超买"
    if rsi <= RSI_OVERSOLD:
        return "超卖"
    return "中性"


def analyze_technicals(snapshot: MarketSnapshot) -> TechnicalContext:
    rsi = snapshot.daily_rsi
    zone = rsi_zone_label(rsi)
    buy_triggered = False
    buy_reason = f"RSI {rsi:.0f}（{zone}），未达买入条件（需 <{RSI_OVERSOLD}）"
    buy_strength = 0.0
    sell_triggered = False
    sell_reason = f"RSI {rsi:.0f}（{zone}），未达卖出条件（需 ≥{RSI_OVERBOUGHT}）"
    sell_strength = 0.0

    if rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        buy_triggered = True
        buy_reason = f"RSI {rsi:.0f} 极端超跌 + 近30日低点 {fmt_jpy(snapshot.low_30d)}"
        buy_strength = 1.0
    elif rsi < RSI_OVERSOLD and snapshot.price <= snapshot.bb_lower * 1.02:
        buy_triggered = True
        buy_reason = f"RSI {rsi:.0f} 超卖 + 价格近布林带下轨 {fmt_jpy(snapshot.bb_lower)}"
        buy_strength = 0.7
    elif rsi < RSI_OVERSOLD:
        buy_triggered = True
        buy_reason = f"RSI {rsi:.0f} 进入超卖区（<{RSI_OVERSOLD}）"
        buy_strength = 0.5

    if rsi >= RSI_OVERBOUGHT and snapshot.price >= snapshot.ma20:
        sell_triggered = True
        sell_reason = f"RSI {rsi:.0f} 超买 + 价格高于 MA20 {fmt_jpy(snapshot.ma20)}"
        sell_strength = 1.0
    elif rsi >= RSI_OVERBOUGHT:
        sell_triggered = True
        sell_reason = f"RSI {rsi:.0f} 超买（≥{RSI_OVERBOUGHT}）"
        sell_strength = 0.75
    elif rsi >= 65 and snapshot.price >= snapshot.ma50 and snapshot.macd_hist < 0:
        sell_triggered = True
        sell_reason = f"RSI {rsi:.0f} 偏高 + MACD 转弱 + 高于 MA50"
        sell_strength = 0.5

    # ストキャス：超卖区ゴールデンクロス / 超买区デッドクロス（RSI 辅助确认）
    if snapshot.stoch_golden and snapshot.stoch_k < STOCH_OVERSOLD:
        if buy_triggered:
            buy_strength = min(1.0, buy_strength + 0.15)
            buy_reason += f" + Stoch 金叉（K={snapshot.stoch_k:.0f} > D={snapshot.stoch_d:.0f}）"
        else:
            buy_triggered = True
            buy_reason = (
                f"Stoch 金叉（K={snapshot.stoch_k:.0f} 上穿 D={snapshot.stoch_d:.0f}）"
                f" + 超卖区（<{STOCH_OVERSOLD}）"
            )
            buy_strength = 0.55

    if snapshot.stoch_dead and snapshot.stoch_k > STOCH_OVERBOUGHT:
        if sell_triggered:
            sell_strength = min(1.0, sell_strength + 0.15)
            sell_reason += f" + Stoch 死叉（K={snapshot.stoch_k:.0f} < D={snapshot.stoch_d:.0f}）"
        else:
            sell_triggered = True
            sell_reason = (
                f"Stoch 死叉（K={snapshot.stoch_k:.0f} 下穿 D={snapshot.stoch_d:.0f}）"
                f" + 超买区（>{STOCH_OVERBOUGHT}）"
            )
            sell_strength = 0.55

    return TechnicalContext(
        rsi=rsi,
        rsi_zone=zone,
        buy_triggered=buy_triggered,
        buy_reason=buy_reason,
        buy_strength=buy_strength,
        sell_triggered=sell_triggered,
        sell_reason=sell_reason,
        sell_strength=sell_strength,
        stoch_k=snapshot.stoch_k,
        stoch_d=snapshot.stoch_d,
        stoch_golden=snapshot.stoch_golden,
        stoch_dead=snapshot.stoch_dead,
    )


def _cycle_amount_scale(cycle: CycleContext, base_strength: float) -> float:
    """周期仅微调仓位，不单独触发买卖。"""
    scale = base_strength
    if cycle.phase in ("筑底区", "回调低吸区"):
        scale = min(1.0, scale + 0.1)
    elif cycle.phase in ("过热区", "周期顶部区"):
        scale *= 0.6
    if cycle.month_strength == "强":
        scale = min(1.0, scale + 0.05)
    elif cycle.month_strength == "弱":
        scale *= 0.85
    return scale


def build_recovery_plan(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    cycle: CycleContext,
    technical: TechnicalContext,
) -> RecoveryPlan:
    p = snapshot.price
    total = portfolio.total_assets(p)
    gap = portfolio.recovery_gap(p)
    pct = portfolio.recovery_pct(p)
    dca_jpy = suggest_dca_jpy(portfolio.cash_jpy)
    milestone, milestone_label = next_milestone(total)

    hold_price = 0.0
    if portfolio.xrp_quantity > 0:
        need = portfolio.target_jpy - portfolio.cash_jpy
        hold_price = max(0.0, need / portfolio.xrp_quantity)

    swing_profit = dca_jpy * SWING_PROFIT_PCT if dca_jpy > 0 else 0.0

    buy_steps: list[PlanStep] = []
    sell_steps: list[PlanStep] = []

    buy_levels = [
        (snapshot.low_30d, "30日低点", dca_jpy, f"RSI<{DAILY_RSI_EXTREME} 且近低点"),
        (cycle.p25_price, "4年 25% 分位", dca_jpy * 0.75 if dca_jpy else 0, f"RSI<{RSI_OVERSOLD} 时参考"),
        (cycle.p50_price, "4年 50% 分位", dca_jpy * 0.5 if dca_jpy else 0, f"RSI<{RSI_OVERSOLD} 时参考"),
        (cycle.range_low, "4年最低点", dca_jpy, f"RSI<{DAILY_RSI_EXTREME} 时参考"),
    ]
    seen_prices: set[int] = set()
    for trigger, label, amount, cond in buy_levels:
        if amount <= 0:
            continue
        key = int(trigger)
        if key in seen_prices:
            continue
        seen_prices.add(key)
        after = portfolio.after_buy(amount, trigger)
        buy_steps.append(
            PlanStep(
                action="参考",
                trigger_price=trigger,
                trigger_label=f"{label}（{cond}）",
                amount_desc=f"若触发 · 约 {fmt_jpy(amount)}",
                result_desc=(
                    f"周期参考位，非立即操作 · 4年阶段 {cycle.phase} · "
                    f"预计总资产 {fmt_jpy(after.total_assets(trigger))}"
                ),
                amount_jpy=amount,
                amount_xrp=amount / trigger if trigger > 0 else 0.0,
            )
        )

    sell_levels = [
        (cycle.p75_price, "4年 75% 分位", SWING_SELL_FRACTION, f"RSI≥{RSI_OVERBOUGHT} 时参考"),
        (cycle.range_high * 0.90, "4年高点 -10%", SWING_SELL_FRACTION, f"RSI≥65 时参考"),
        (cycle.range_high, "4年最高点", SWING_SELL_FRACTION_HIGH, f"RSI≥{RSI_OVERBOUGHT} 分批"),
        (snapshot.high_30d, "30日高点", SWING_SELL_FRACTION, f"RSI≥{RSI_OVERBOUGHT} 时参考"),
    ]
    seen_sell: set[int] = set()
    for trigger, label, fraction, cond in sell_levels:
        key = int(trigger)
        if key in seen_sell:
            continue
        seen_sell.add(key)
        sell_qty = portfolio.xrp_quantity * fraction
        proceeds = sell_qty * trigger
        sell_steps.append(
            PlanStep(
                action="参考",
                trigger_price=trigger,
                trigger_label=f"{label}（{cond}）",
                amount_desc=f"若触发 · 约卖 {sell_qty:,.0f} XRP",
                result_desc=(
                    f"周期参考位，非立即操作 · 4年阶段 {cycle.phase} · "
                    f"落袋约 {fmt_jpy(proceeds)}"
                ),
                amount_jpy=proceeds,
                amount_xrp=sell_qty,
            )
        )

    buy_steps.sort(key=lambda s: s.trigger_price)
    sell_steps.sort(key=lambda s: s.trigger_price)

    return RecoveryPlan(
        total_assets=total,
        target_jpy=portfolio.target_jpy,
        recovery_gap=gap,
        recovery_pct=pct,
        next_milestone=milestone,
        next_milestone_label=milestone_label,
        dca_buy_jpy=dca_jpy,
        buy_steps=tuple(buy_steps),
        sell_steps=tuple(sell_steps),
        hold_only_price=hold_price,
        swing_cycle_profit=swing_profit,
        cycle=cycle,
        technical=technical,
    )


def generate_trade_advice(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
) -> list[TradeAdvice]:
    advice: list[TradeAdvice] = []
    p = snapshot.price
    dca_jpy = plan.dca_buy_jpy
    total = plan.total_assets
    cycle = plan.cycle
    tech = plan.technical

    if portfolio.target_jpy <= 0:
        advice.append(
            TradeAdvice(
                action="配置",
                strength="请先",
                title="设置回本目标",
                reason="尚未配置原始投入目标",
                detail="在侧边栏填写回本目标金额（TARGET_JPY）",
            )
        )
        return advice

    advice.append(
        TradeAdvice(
            action="持有",
            strength="参考",
            title=f"4年周期 · {cycle.phase}（{cycle.position_pct:.0f}%）",
            reason=(
                f"{cycle.halving_label} · {cycle.month}月{cycle.month_strength} · "
                f"区间 {fmt_jpy(cycle.range_low)}–{fmt_jpy(cycle.range_high)}"
            ),
            detail=f"{cycle.phase_detail} · {cycle.month_history}（仅供参考，不单独触发买卖）",
        )
    )

    advice.append(
        TradeAdvice(
            action="持有",
            strength="技术",
            title=f"RSI {tech.rsi:.0f}（{tech.rsi_zone}）· MA20 {fmt_jpy(snapshot.ma20)}",
            reason=f"买入：{tech.buy_reason}",
            detail=f"卖出：{tech.sell_reason}",
        )
    )

    if tech.buy_triggered and dca_jpy > 0:
        strength = _cycle_amount_scale(cycle, tech.buy_strength)
        amount = dca_jpy * strength
        advice.append(
            TradeAdvice(
                action="买入",
                strength="强烈建议" if tech.buy_strength >= 0.9 else "建议",
                title="技术信号 · 执行买入",
                reason=tech.buy_reason,
                detail=(
                    f"建议投入 {fmt_jpy(amount)} 买入约 {amount / p:.1f} XRP。"
                    f"周期参考：{cycle.phase}，可挂低于现价位 "
                    f"{fmt_jpy(cycle.p25_price)} / {fmt_jpy(cycle.range_low)}。"
                ),
            )
        )
    elif tech.buy_triggered:
        advice.append(
            TradeAdvice(
                action="买入",
                strength="信号",
                title="技术达标 · 现金不足",
                reason=tech.buy_reason,
                detail="技术条件已满足，但当前无可用现金。",
            )
        )

    if tech.sell_triggered:
        fraction = SWING_SELL_FRACTION * tech.sell_strength
        sell_qty = portfolio.xrp_quantity * max(SWING_SELL_FRACTION * 0.5, fraction)
        advice.append(
            TradeAdvice(
                action="卖出",
                strength="建议" if tech.sell_strength >= 0.75 else "可考虑",
                title="技术信号 · 分批卖出",
                reason=tech.sell_reason,
                detail=(
                    f"建议卖出约 {sell_qty:,.0f} XRP（{fmt_jpy(sell_qty * p)}）。"
                    f"周期参考卖点 {fmt_jpy(cycle.p75_price)} / {fmt_jpy(cycle.range_high)}。"
                ),
            )
        )

    if not any(a.action in ("买入", "卖出") for a in advice[2:]):
        next_buy = plan.buy_steps[0] if plan.buy_steps else None
        next_sell = plan.sell_steps[0] if plan.sell_steps else None
        parts = [tech.buy_reason]
        if next_buy:
            parts.append(f"周期参考买 {fmt_jpy(next_buy.trigger_price)}")
        if next_sell:
            parts.append(f"周期参考卖 {fmt_jpy(next_sell.trigger_price)}")
        advice.append(
            TradeAdvice(
                action="等待",
                strength="当前",
                title="技术未触发 · 持有观望",
                reason=f"RSI {tech.rsi:.0f}（{tech.rsi_zone}）· 资产目标 {plan.next_milestone_label}",
                detail=" · ".join(parts),
            )
        )

    return advice


def build_current_action(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
) -> CurrentAction:
    p = snapshot.price
    dca_jpy = plan.dca_buy_jpy
    cycle = plan.cycle
    tech = plan.technical
    next_buy = plan.buy_steps[0] if plan.buy_steps else None
    next_sell = plan.sell_steps[0] if plan.sell_steps else None
    nb = next_buy.trigger_price if next_buy else cycle.p25_price
    ns = next_sell.trigger_price if next_sell else cycle.p75_price

    if tech.sell_triggered:
        fraction = max(SWING_SELL_FRACTION * 0.5, SWING_SELL_FRACTION * tech.sell_strength)
        sell_qty = portfolio.xrp_quantity * fraction
        return CurrentAction(
            action="卖出",
            title="技术信号 · 分批卖出",
            reason=f"{tech.sell_reason} · 周期参考 {cycle.phase}",
            buy_jpy=0.0,
            buy_xrp=0.0,
            sell_xrp=sell_qty,
            sell_jpy=sell_qty * p,
            next_buy_price=nb,
            next_sell_price=ns,
            trigger_price=p,
        )

    if tech.buy_triggered and dca_jpy > 0:
        strength = _cycle_amount_scale(cycle, tech.buy_strength)
        amount = dca_jpy * strength
        return CurrentAction(
            action="买入",
            title="技术信号 · 执行买入",
            reason=f"{tech.buy_reason} · 周期参考 {cycle.phase}",
            buy_jpy=amount,
            buy_xrp=amount / p if p > 0 else 0.0,
            sell_xrp=0.0,
            sell_jpy=0.0,
            next_buy_price=nb,
            next_sell_price=ns,
            trigger_price=p,
        )

    if tech.buy_triggered:
        return CurrentAction(
            action="等待",
            title="技术达标 · 现金不足",
            reason=tech.buy_reason,
            buy_jpy=0.0,
            buy_xrp=0.0,
            sell_xrp=0.0,
            sell_jpy=0.0,
            next_buy_price=nb,
            next_sell_price=ns,
            trigger_price=None,
        )

    return CurrentAction(
        action="等待",
        title="技术未触发 · 持有观望",
        reason=f"RSI {tech.rsi:.0f}（{tech.rsi_zone}）· 周期 {cycle.phase} 仅供参考",
        buy_jpy=0.0,
        buy_xrp=0.0,
        sell_xrp=0.0,
        sell_jpy=0.0,
        next_buy_price=nb,
        next_sell_price=ns,
        trigger_price=None,
    )


def detect_signals(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
) -> list[Signal]:
    signals: list[Signal] = []
    p = snapshot.price
    cycle = plan.cycle
    tech = plan.technical
    action = build_current_action(snapshot, portfolio, plan)

    if action.action == "买入" and action.buy_jpy > 0:
        msg = f"💡【技术买点】{tech.buy_reason} · 建议 {fmt_jpy(action.buy_jpy)}"
        signals.append(
            Signal(
                key="tech_buy",
                title="RSI 买点",
                console_msg=msg,
                card_template="green",
                card_body=(
                    f"{msg}\n\n"
                    f"**价格：** {fmt_jpy(p)} · RSI {tech.rsi:.0f}\n"
                    f"**周期参考：** {cycle.phase}（{cycle.position_pct:.0f}%）\n"
                    f"**4年区间：** {fmt_jpy(cycle.range_low)} – {fmt_jpy(cycle.range_high)}"
                ),
                color=f"{Fore.GREEN}{Style.BRIGHT}",
            )
        )

    if action.action == "卖出" and action.sell_xrp > 0:
        msg = f"📤【技术卖点】{tech.sell_reason} · 约 {action.sell_xrp:,.0f} XRP"
        signals.append(
            Signal(
                key="tech_sell",
                title="RSI 卖点",
                console_msg=msg,
                card_template="blue",
                card_body=(
                    f"{msg}\n\n"
                    f"**价格：** {fmt_jpy(p)} · RSI {tech.rsi:.0f}\n"
                    f"**周期参考：** {cycle.phase}\n"
                    f"**回收约：** {fmt_jpy(action.sell_jpy)}"
                ),
                color=f"{Fore.CYAN}{Style.BRIGHT}",
            )
        )

    if plan.recovery_gap <= 0:
        msg = f"🎉【回本】总资产已达 {fmt_jpy(plan.total_assets)}，超过投入目标！"
        signals.append(
            Signal(
                key="target_reached",
                title="投入目标达成",
                console_msg=msg,
                card_template="orange",
                card_body=(
                    f"{msg}\n\n"
                    f"**XRP：** {portfolio.xrp_quantity:,.0f} 枚 · **现金：** {fmt_jpy(portfolio.cash_jpy)}\n"
                    f"**总盈亏：** {fmt_jpy(portfolio.pnl(p))}"
                ),
                color=f"{Fore.YELLOW}{Style.BRIGHT}",
            )
        )

    return signals


def _feishu_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    return base64.b64encode(
        hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")


def send_lark_card(signal: Signal, snapshot: MarketSnapshot) -> None:
    if not FEISHU_WEBHOOK_URL:
        return

    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    payload: dict = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": signal.card_template,
                "title": {
                    "tag": "plain_text",
                    "content": f"📊 {PAIR_LABEL} · {signal.title}",
                },
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": signal.card_body},
                },
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"数据源: {snapshot.data_source} · {now}",
                        }
                    ],
                },
            ],
        },
    }

    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _feishu_sign(timestamp, FEISHU_SECRET)

    response = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)
    response.raise_for_status()
    result = response.json()
    if result.get("code", 0) != 0:
        raise requests.RequestException(result.get("msg", "飞书推送失败"))


def push_signals(
    signals: list[Signal],
    snapshot: MarketSnapshot,
    cooldown: AlertCooldown,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if is_quiet_hours():
        for signal in signals:
            results.append(
                {"key": signal.key, "title": signal.title, "status": "quiet_hours"}
            )
        return results

    for signal in signals:
        if not cooldown.can_send(signal.key):
            results.append(
                {
                    "key": signal.key,
                    "title": signal.title,
                    "status": "cooldown",
                    "remaining_hours": cooldown.remaining_hours(signal.key),
                }
            )
            continue
        try:
            send_lark_card(signal, snapshot)
            cooldown.mark_sent(signal.key)
            results.append(
                {"key": signal.key, "title": signal.title, "status": "sent"}
            )
            print(
                f"{Fore.GREEN}[已推送 Lark] {signal.title}"
                f"（24 小时内不重复）{Style.RESET_ALL}"
            )
        except requests.RequestException as exc:
            results.append(
                {
                    "key": signal.key,
                    "title": signal.title,
                    "status": "failed",
                    "error": str(exc),
                }
            )
            print(f"{Fore.RED}[Lark 推送失败] {exc}{Style.RESET_ALL}")
    return results


def run_monitor_cycle(
    cooldown: AlertCooldown | None = None,
    portfolio: Portfolio | None = None,
) -> dict[str, Any]:
    cd = cooldown or AlertCooldown(ALERT_COOLDOWN_SECONDS)
    pf = portfolio or load_portfolio()
    snapshot, daily = build_snapshot()
    cycle = analyze_cycle(daily, snapshot.price)
    technical = analyze_technicals(snapshot)
    plan = build_recovery_plan(snapshot, pf, cycle, technical)
    current_action = build_current_action(snapshot, pf, plan)
    advice = generate_trade_advice(snapshot, pf, plan)
    signals = detect_signals(snapshot, pf, plan)
    push_results = push_signals(signals, snapshot, cd)
    return {
        "snapshot": snapshot,
        "daily": daily,
        "portfolio": pf,
        "cycle": cycle,
        "technical": technical,
        "recovery_plan": plan,
        "current_action": current_action,
        "advice": advice,
        "chart_data": build_chart_data(daily, cycle),
        "signals": signals,
        "push_results": push_results,
        "cooldown": cd,
        "updated_at": now_local(),
    }


def print_advice(advice: list[TradeAdvice]) -> None:
    colors = {
        "买入": Fore.GREEN,
        "卖出": Fore.YELLOW,
        "持有": Fore.CYAN,
        "观望": Fore.WHITE,
        "配置": Fore.RED,
    }
    for item in advice:
        color = colors.get(item.action, Fore.WHITE)
        print(
            f"{color}{Style.BRIGHT}[{item.action}] {item.strength} · {item.title}{Style.RESET_ALL}"
        )
        print(f"  {item.reason}")
        print(f"  → {item.detail}")


def print_signals(signals: list[Signal], cooldown: AlertCooldown) -> None:
    if not signals:
        print(f"{Fore.WHITE}暂无 Lark 推送信号{Style.RESET_ALL}")
        return
    for signal in signals:
        remain = cooldown.remaining_hours(signal.key)
        suffix = f"（已推送，冷却 {remain:.1f}h）" if remain > 0 else ""
        print(f"{signal.color}{signal.console_msg}{suffix}{Style.RESET_ALL}")


def print_dashboard(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
    advice: list[TradeAdvice],
    signals: list[Signal],
    cooldown: AlertCooldown,
) -> None:
    clear_screen()
    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    push_status = "飞书 Lark" if FEISHU_WEBHOOK_URL else "未配置"
    p = snapshot.price
    pnl = portfolio.pnl(p)

    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  {PAIR_LABEL} 回本波段计划{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"更新时间: {now}  |  数据源: {snapshot.data_source}")
    print(f"推送: {push_status}  |  RSI: {snapshot.daily_rsi:.1f}")
    print("-" * 54)
    print(f"当前价格: {Fore.WHITE}{Style.BRIGHT}{fmt_jpy(p)}{Style.RESET_ALL}")
    print("-" * 54)
    print("资产概况:")
    print(f"  XRP: {portfolio.xrp_quantity:,.0f} 枚 ({fmt_jpy(portfolio.xrp_value(p))})")
    print(f"  现金: {fmt_jpy(portfolio.cash_jpy)}")
    print(f"  总资产: {Fore.WHITE}{Style.BRIGHT}{fmt_jpy(plan.total_assets)}{Style.RESET_ALL}")
    print(f"  目标: {fmt_jpy(portfolio.target_jpy)}  |  还差: {fmt_jpy(plan.recovery_gap)}")
    pnl_color = Fore.GREEN if pnl >= 0 else Fore.RED
    print(f"  总盈亏: {pnl_color}{fmt_jpy(pnl)} ({portfolio.pnl_pct(p):+.1f}%){Style.RESET_ALL}")
    print(f"  进度: {plan.recovery_pct * 100:.1f}% → 下一目标 {plan.next_milestone_label}")
    print("-" * 54)
    print("买卖建议:")
    print_advice(advice)
    print("-" * 54)
    if plan.buy_steps:
        print("低吸计划:")
        for step in plan.buy_steps:
            print(f"  {fmt_jpy(step.trigger_price)} {step.trigger_label}: {step.amount_desc}")
    if plan.sell_steps:
        print("高抛计划:")
        for step in plan.sell_steps:
            print(f"  {fmt_jpy(step.trigger_price)} {step.trigger_label}: {step.amount_desc}")
    print("-" * 54)
    print("Lark 信号:")
    print_signals(signals, cooldown)
    print(f"\n{Fore.WHITE}按 Ctrl+C 退出{Style.RESET_ALL}")


def _configure_console_encoding() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    _configure_console_encoding()
    init(autoreset=True)
    cooldown = AlertCooldown(ALERT_COOLDOWN_SECONDS)
    portfolio = load_portfolio()
    print(f"正在启动 {PAIR_LABEL} 回本波段监控...")

    while True:
        try:
            result = run_monitor_cycle(cooldown, portfolio)
            print_dashboard(
                result["snapshot"],
                result["portfolio"],
                result["recovery_plan"],
                result["advice"],
                result["signals"],
                result["cooldown"],
            )
        except requests.RequestException as exc:
            clear_screen()
            print(f"{Fore.RED}网络请求失败: {exc}{Style.RESET_ALL}")
            print(f"{REFRESH_SECONDS} 秒后重试...")
        except (ValueError, IndexError, KeyError) as exc:
            clear_screen()
            print(f"{Fore.RED}数据处理失败: {exc}{Style.RESET_ALL}")
            print(f"{REFRESH_SECONDS} 秒后重试...")

        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            print("\n程序已退出。")
            sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n程序已退出。")
        sys.exit(0)
