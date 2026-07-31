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
from datetime import datetime
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
    data_source: str


@dataclass(frozen=True)
class PlanStep:
    action: str
    trigger_price: float
    trigger_label: str
    amount_desc: str
    result_desc: str


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


def fetch_bitbank_daily() -> pd.DataFrame:
    current_year = now_local().year
    years = {current_year - 1, current_year}
    frames: list[pd.DataFrame] = []

    for year in sorted(years):
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
        raise requests.RequestException("Bitbank 日线数据为空")

    daily = pd.concat(frames, ignore_index=True)
    daily = daily.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return daily.reset_index(drop=True)


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
        data_source=source,
    )


def build_chart_data(daily: pd.DataFrame, limit: int = 60) -> pd.DataFrame:
    work = daily.copy().tail(limit)
    work["date"] = pd.to_datetime(work["timestamp"], unit="ms", errors="coerce")
    if work["date"].isna().all():
        work["date"] = pd.RangeIndex(len(work))
    chart = work.set_index("date")
    chart["price"] = chart["close"]
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
        return _snapshot_from_daily(price, binance_daily, "Binance"), binance_daily
    except requests.RequestException as exc:
        errors.append(f"Binance: {exc}")

    raise requests.RequestException(" / ".join(errors))


def at_30d_low(snapshot: MarketSnapshot) -> bool:
    threshold = snapshot.low_30d * (1 + LOW_TOUCH_TOLERANCE)
    return snapshot.price <= threshold



def build_recovery_plan(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
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
        (snapshot.low_30d, "30日低点", dca_jpy, "RSI<25 极端超跌"),
        (snapshot.bb_lower, "布林带下轨", dca_jpy / 2 if dca_jpy else 0, "RSI<30 超卖"),
        (p * 0.97, "当前价 -3%", dca_jpy / 3 if dca_jpy else 0, "回调分批"),
    ]
    for trigger, label, amount, cond in buy_levels:
        if amount <= 0:
            continue
        after = portfolio.after_buy(amount, trigger)
        buy_steps.append(
            PlanStep(
                action="买入",
                trigger_price=trigger,
                trigger_label=f"{label}（{cond}）",
                amount_desc=f"投入 {fmt_jpy(amount)}",
                result_desc=(
                    f"预计总资产 {fmt_jpy(after.total_assets(trigger))} · "
                    f"距目标还差 {fmt_jpy(portfolio.target_jpy - after.total_assets(trigger))}"
                ),
            )
        )

    sell_levels = [
        (snapshot.ma20, "MA20", SWING_SELL_FRACTION, "反弹第一阻力"),
        (snapshot.ma50, "MA50", SWING_SELL_FRACTION, "反弹第二阻力"),
        (snapshot.high_30d, "30日高点", SWING_SELL_FRACTION_HIGH, "RSI>65 分批止盈"),
    ]
    for trigger, label, fraction, cond in sell_levels:
        if trigger <= p:
            continue
        sell_qty = portfolio.xrp_quantity * fraction
        proceeds = sell_qty * trigger
        after = portfolio.after_sell(sell_qty, trigger)
        sell_steps.append(
            PlanStep(
                action="卖出",
                trigger_price=trigger,
                trigger_label=f"{label}（{cond}）",
                amount_desc=f"卖出 {sell_qty:,.0f} XRP → {fmt_jpy(proceeds)}",
                result_desc=(
                    f"落袋现金 {fmt_jpy(after.cash_jpy)} · "
                    f"剩余 {after.xrp_quantity:,.0f} XRP · "
                    f"总资产 {fmt_jpy(after.total_assets(trigger))}"
                ),
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
    )


def generate_trade_advice(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
) -> list[TradeAdvice]:
    advice: list[TradeAdvice] = []
    p = snapshot.price
    rsi = snapshot.daily_rsi
    dca_jpy = plan.dca_buy_jpy
    total = plan.total_assets

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
            strength="总览",
            title=f"目标 {fmt_jpy(portfolio.target_jpy)} · 当前 {fmt_jpy(total)}",
            reason=f"总盈亏 {fmt_jpy(portfolio.pnl(p))}（{portfolio.pnl_pct(p):+.1f}%）· 还差 {fmt_jpy(plan.recovery_gap)}",
            detail=(
                f"纯持有需 XRP 涨至约 {fmt_jpy(plan.hold_only_price)}/枚 才能达到投入目标；"
                f"仅靠 {fmt_jpy(portfolio.cash_jpy)} 现金波段每次约赚 {fmt_jpy(plan.swing_cycle_profit)}，"
                f"需配合价格上涨 + 低吸高抛。"
            ),
        )
    )

    if rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        if dca_jpy > 0:
            after = portfolio.after_buy(dca_jpy, p)
            advice.append(
                TradeAdvice(
                    action="买入",
                    strength="强烈建议",
                    title="极端超跌 · 执行低吸",
                    reason=f"RSI {rsi:.1f}，价格贴近 30 日低点 {fmt_jpy(snapshot.low_30d)}",
                    detail=(
                        f"用 {fmt_jpy(dca_jpy)}（现金 1/3）买入约 {dca_jpy / p:.1f} XRP。"
                        f"买入后总持仓 {after.xrp_quantity:,.0f} 枚，"
                        f"总资产 {fmt_jpy(after.total_assets(p))}。"
                        f"等反弹至 MA20 {fmt_jpy(snapshot.ma20)} 再卖 15% 做波段。"
                    ),
                )
            )
        else:
            advice.append(
                TradeAdvice(
                    action="买入",
                    strength="信号出现",
                    title="极端超跌 · 但现金不足",
                    reason=f"RSI {rsi:.1f}，价格处于低位",
                    detail="当前现金为 0，无法执行低吸。如有余力可补充日元。",
                )
            )
    elif rsi < RSI_OVERSOLD and p <= snapshot.bb_lower * 1.02 and dca_jpy > 0:
        half = dca_jpy / 2
        advice.append(
            TradeAdvice(
                action="买入",
                strength="可考虑",
                title="超卖区 · 小仓试探",
                reason=f"RSI {rsi:.1f}，接近布林带下轨",
                detail=f"非极端低位，建议只用 {fmt_jpy(half)} 试探，保留子弹等更深回调。",
            )
        )

    for step in plan.sell_steps[:2]:
        if p >= step.trigger_price * 0.98:
            advice.append(
                TradeAdvice(
                    action="卖出",
                    strength="准备",
                    title=f"接近 {step.trigger_label}",
                    reason=f"价格 {fmt_jpy(p)} 逼近卖出位 {fmt_jpy(step.trigger_price)}",
                    detail=step.amount_desc + "。" + step.result_desc + "。卖出后等下次超跌再买回。",
                )
            )
            break

    if rsi >= RSI_OVERBOUGHT and p >= snapshot.ma20:
        sell_qty = portfolio.xrp_quantity * SWING_SELL_FRACTION
        advice.append(
            TradeAdvice(
                action="卖出",
                strength="建议",
                title="超买反弹 · 分批高抛",
                reason=f"RSI {rsi:.1f}，价格高于 MA20",
                detail=(
                    f"卖出约 {sell_qty:,.0f} XRP（15%）锁定 {fmt_jpy(sell_qty * p)} 现金。"
                    f"不追求一次回本，积少成多，等下次低位接回。"
                ),
            )
        )

    if not any(a.action in ("买入", "卖出") for a in advice[1:]):
        next_buy = plan.buy_steps[0] if plan.buy_steps else None
        next_sell = plan.sell_steps[0] if plan.sell_steps else None
        parts = []
        if next_buy:
            parts.append(f"低吸位 {fmt_jpy(next_buy.trigger_price)}（{next_buy.trigger_label}）")
        if next_sell:
            parts.append(f"高抛位 {fmt_jpy(next_sell.trigger_price)}（{next_sell.trigger_label}）")
        advice.append(
            TradeAdvice(
                action="观望",
                strength="当前",
                title="等待触发 · 勿频繁操作",
                reason=f"下一目标：{plan.next_milestone_label}（还差 {fmt_jpy(plan.next_milestone - total)}）",
                detail=" · ".join(parts) if parts else "保持现有仓位，按 plan 表执行。",
            )
        )

    return advice


def detect_signals(
    snapshot: MarketSnapshot,
    portfolio: Portfolio,
    plan: RecoveryPlan,
) -> list[Signal]:
    signals: list[Signal] = []
    p = snapshot.price
    dca_jpy = plan.dca_buy_jpy

    if snapshot.daily_rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        msg = (
            f"💡【极端超跌】可用 {fmt_jpy(dca_jpy)} 低吸，"
            f"目标 {plan.next_milestone_label}（当前 {fmt_jpy(plan.total_assets)}）"
            if dca_jpy > 0
            else "💡【极端超跌】价格处于低位，但当前现金不足"
        )
        signals.append(
            Signal(
                key="extreme_oversold",
                title="极端超跌 · 低吸",
                console_msg=msg,
                card_template="green",
                card_body=(
                    f"{msg}\n\n"
                    f"**价格：** {fmt_jpy(p)} · RSI {snapshot.daily_rsi:.1f}\n"
                    f"**总资产：** {fmt_jpy(plan.total_assets)} / 目标 {fmt_jpy(plan.target_jpy)}\n"
                    f"**还差：** {fmt_jpy(plan.recovery_gap)}"
                ),
                color=f"{Fore.GREEN}{Style.BRIGHT}",
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
    plan = build_recovery_plan(snapshot, pf)
    advice = generate_trade_advice(snapshot, pf, plan)
    signals = detect_signals(snapshot, pf, plan)
    push_results = push_signals(signals, snapshot, cd)
    return {
        "snapshot": snapshot,
        "daily": daily,
        "portfolio": pf,
        "recovery_plan": plan,
        "advice": advice,
        "chart_data": build_chart_data(daily),
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
