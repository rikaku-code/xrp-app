#!/usr/bin/env python3
"""XRP/JPY 长期持仓监控 — 持仓回本分析与买卖建议。"""

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

# ── 持仓（通过 .env 或 Streamlit Secrets 配置）──────────────
DEFAULT_HOLDINGS_XRP = 1000.0
DEFAULT_HOLDINGS_AVG_COST = 210.0
DEFAULT_AVAILABLE_JPY = 90_000.0
DCA_FRACTION = 1 / 3

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
class Position:
    """持仓：数量 + 持仓均价（日元/XRP）。"""

    quantity: float
    avg_cost_jpy: float

    @property
    def avg_cost(self) -> float:
        return self.avg_cost_jpy

    @property
    def total_cost_jpy(self) -> float:
        return self.quantity * self.avg_cost_jpy

    def market_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return self.market_value(price) - self.total_cost_jpy

    def pnl_pct(self, price: float) -> float:
        if self.total_cost_jpy <= 0:
            return 0.0
        return self.unrealized_pnl(price) / self.total_cost_jpy * 100

    def dca_preview(self, buy_jpy: float, buy_price: float) -> "Position":
        if buy_price <= 0 or buy_jpy <= 0:
            return self
        added_qty = buy_jpy / buy_price
        new_qty = self.quantity + added_qty
        new_avg = (self.total_cost_jpy + buy_jpy) / new_qty
        return Position(quantity=new_qty, avg_cost_jpy=new_avg)


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
class RecoveryPlan:
    breakeven_price: float
    distance_jpy: float
    distance_pct: float
    recover_principal_qty: float
    dca_breakeven_after: float
    dca_buy_jpy: float
    milestone_50pct: float
    milestone_80pct: float
    sell_target_conservative: float
    sell_target_aggressive: float


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


def load_position(
    quantity: float | None = None,
    avg_cost_jpy: float | None = None,
) -> Position:
    qty = quantity if quantity is not None else float(
        os.getenv("HOLDINGS_XRP", DEFAULT_HOLDINGS_XRP)
    )
    avg = avg_cost_jpy if avg_cost_jpy is not None else float(
        os.getenv("HOLDINGS_AVG_COST", DEFAULT_HOLDINGS_AVG_COST)
    )
    return Position(quantity=max(0.0, qty), avg_cost_jpy=max(0.0, avg))


def load_available_jpy(available_jpy: float | None = None) -> float:
    if available_jpy is not None:
        return max(0.0, available_jpy)
    return max(0.0, float(os.getenv("AVAILABLE_JPY", DEFAULT_AVAILABLE_JPY)))


def suggest_dca_jpy(available_jpy: float) -> float:
    """单次加仓建议：可支配资金的 1/3。"""
    if available_jpy <= 0:
        return 0.0
    return available_jpy * DCA_FRACTION


COOLDOWN_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".alert_cooldown.json"
)


class AlertCooldown:
    """同一信号类型在冷却期内最多推送一次。"""

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
    return f"¥{value:,.2f}"


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


def build_chart_data(
    daily: pd.DataFrame,
    position: Position,
    limit: int = 60,
) -> pd.DataFrame:
    work = daily.copy()
    chart = work.tail(limit).copy()
    chart["date"] = pd.to_datetime(chart["timestamp"], unit="ms", errors="coerce")
    if chart["date"].isna().all():
        chart["date"] = pd.RangeIndex(len(chart))
    chart = chart.set_index("date")
    chart["price"] = chart["close"]
    if position.avg_cost > 0:
        chart["breakeven"] = position.avg_cost
    return chart[["price", "breakeven"]] if position.avg_cost > 0 else chart[["price"]]


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
    position: Position,
    available_jpy: float,
) -> RecoveryPlan:
    be = position.avg_cost
    dist = snapshot.price - be
    dist_pct = (dist / be * 100) if be > 0 else 0.0
    dca_jpy = suggest_dca_jpy(available_jpy)

    recover_qty = (
        position.total_cost_jpy / snapshot.price if snapshot.price > 0 else 0.0
    )
    after_dca = position.dca_preview(dca_jpy, snapshot.price)

    loss = max(0.0, -position.unrealized_pnl(snapshot.price))
    milestone_50 = snapshot.price + (loss * 0.5 / position.quantity if position.quantity > 0 else 0)
    milestone_80 = snapshot.price + (loss * 0.8 / position.quantity if position.quantity > 0 else 0)

    return RecoveryPlan(
        breakeven_price=be,
        distance_jpy=dist,
        distance_pct=dist_pct,
        recover_principal_qty=min(recover_qty, position.quantity),
        dca_breakeven_after=after_dca.avg_cost,
        dca_buy_jpy=dca_jpy,
        milestone_50pct=milestone_50,
        milestone_80pct=milestone_80,
        sell_target_conservative=max(be, snapshot.ma20),
        sell_target_aggressive=min(snapshot.high_30d, be * 1.15) if be > 0 else snapshot.high_30d,
    )


def generate_trade_advice(
    snapshot: MarketSnapshot,
    position: Position,
    plan: RecoveryPlan,
) -> list[TradeAdvice]:
    advice: list[TradeAdvice] = []
    p = snapshot.price
    be = plan.breakeven_price
    rsi = snapshot.daily_rsi

    dca_jpy = plan.dca_buy_jpy

    if position.quantity <= 0 or position.avg_cost_jpy <= 0:
        advice.append(
            TradeAdvice(
                action="配置",
                strength="请先",
                title="设置持仓",
                reason="尚未配置 XRP 数量与持仓均价",
                detail="在侧边栏或 .env 填写 HOLDINGS_XRP 和 HOLDINGS_AVG_COST",
            )
        )
        return advice

    if rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        if dca_jpy > 0:
            after = position.dca_preview(dca_jpy, p)
            advice.append(
                TradeAdvice(
                    action="买入",
                    strength="强烈建议",
                    title="极端超跌 · 分批加仓",
                    reason=f"RSI {rsi:.1f} 且价格贴近 30 日低点",
                    detail=(
                        f"可用约 {fmt_jpy(dca_jpy)}（可支配资金的 1/3）挂限价单低吸。"
                        f"若成交，持仓均价将从 {fmt_jpy(be)} 降至约 {fmt_jpy(after.avg_cost)}，"
                        f"更快接近回本。"
                    ),
                )
            )
        else:
            advice.append(
                TradeAdvice(
                    action="买入",
                    strength="强烈建议",
                    title="极端超跌 · 分批加仓",
                    reason=f"RSI {rsi:.1f} 且价格贴近 30 日低点",
                    detail="当前可支配资金为 0，请补充日元后再考虑加仓。",
                )
            )
    elif rsi < RSI_OVERSOLD and p <= snapshot.bb_lower * 1.02:
        small_buy = dca_jpy / 2 if dca_jpy > 0 else 0.0
        if small_buy > 0:
            after = position.dca_preview(small_buy, p)
            advice.append(
                TradeAdvice(
                    action="买入",
                    strength="可考虑",
                    title="超卖区 · 小仓补仓",
                    reason=f"RSI {rsi:.1f}，价格接近布林带下轨",
                    detail=(
                        f"非极端低位，建议只用约 {fmt_jpy(small_buy)}。"
                        f"补仓后均价约 {fmt_jpy(after.avg_cost)}。"
                    ),
                )
            )

    if p >= be and position.unrealized_pnl(p) >= 0:
        sell_qty = plan.recover_principal_qty
        advice.append(
            TradeAdvice(
                action="卖出",
                strength="建议",
                title="已达回本价 · 回收本金",
                reason=f"当前 {fmt_jpy(p)} ≥ 均价 {fmt_jpy(be)}",
                detail=(
                    f"可卖出约 {sell_qty:,.1f} XRP 回收 {fmt_jpy(position.total_cost_jpy)} 本金，"
                    f"剩余 {position.quantity - sell_qty:,.1f} XRP 当作零成本持仓继续拿。"
                ),
            )
        )
        if rsi >= RSI_OVERBOUGHT:
            advice.append(
                TradeAdvice(
                    action="卖出",
                    strength="可选",
                    title="超买区 · 分批止盈",
                    reason=f"RSI {rsi:.1f} 进入超买，且已回本",
                    detail=(
                        f"可在 {fmt_jpy(plan.sell_target_aggressive)} 附近再卖 20–30%，"
                        f"锁定部分利润，降低回撤风险。"
                    ),
                )
            )
    elif p < be:
        gap = be - p
        advice.append(
            TradeAdvice(
                action="持有",
                strength="当前",
                title="尚未回本 · 耐心持有",
                reason=f"距回本价还差 {fmt_jpy(gap)}（{abs(plan.distance_pct):.1f}%）",
                detail=(
                    f"回本目标价 {fmt_jpy(be)}（{position.quantity:,.1f} XRP × 均价 {fmt_jpy(be)}）。"
                    + (
                        f"若用 {fmt_jpy(dca_jpy)} 补仓，均价可降至 {fmt_jpy(plan.dca_breakeven_after)}。"
                        if dca_jpy > 0
                        else "当前无可支配资金，暂不建议加仓。"
                    )
                ),
            )
        )
        if p >= plan.milestone_50pct and p < be:
            advice.append(
                TradeAdvice(
                    action="持有",
                    strength="进展",
                    title="反弹途中 · 勿追涨杀跌",
                    reason=f"已从低点反弹，但仍低于回本价 {fmt_jpy(be)}",
                    detail="反弹阶段不建议割肉；等待回本价或极端超跌再加仓。",
                )
            )

    if snapshot.macd_hist > 0 and p > snapshot.ma20 and p < be:
        advice.append(
            TradeAdvice(
                action="观望",
                strength="短期",
                title="趋势转强 · 暂不加仓",
                reason="MACD 多头且站上 MA20，但未到回本价",
                detail=f"等价格接近 {fmt_jpy(be)} 或再次极端超跌时再操作。",
            )
        )

    if not advice:
        advice.append(
            TradeAdvice(
                action="观望",
                strength="当前",
                title="无明确信号",
                reason="市场处于常规波动区间",
                detail=f"继续持有，关注回本价 {fmt_jpy(be)} 与 30 日区间 {fmt_jpy(snapshot.low_30d)}–{fmt_jpy(snapshot.high_30d)}。",
            )
        )

    return advice


def detect_signals(
    snapshot: MarketSnapshot,
    position: Position,
    plan: RecoveryPlan,
) -> list[Signal]:
    signals: list[Signal] = []
    p = snapshot.price
    be = plan.breakeven_price

    dca_jpy = plan.dca_buy_jpy

    if snapshot.daily_rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        after = position.dca_preview(dca_jpy, p) if dca_jpy > 0 else position
        msg = (
            "💡【极端超跌】价格处于 30 日低位，"
            + (
                f"可用约 {fmt_jpy(dca_jpy)}（可支配 1/3）分批买入拉低均价。"
                if dca_jpy > 0
                else "可考虑分批买入，但当前可支配资金为 0。"
            )
        )
        signals.append(
            Signal(
                key="extreme_oversold",
                title="极端超跌 · 加仓机会",
                console_msg=msg,
                card_template="green",
                card_body=(
                    f"{msg}\n\n"
                    f"**当前价格：** {fmt_jpy(p)}\n"
                    f"**日线 RSI：** {snapshot.daily_rsi:.1f}\n"
                    f"**持仓均价：** {fmt_jpy(be)}\n"
                    f"**补仓后预估均价：** {fmt_jpy(after.avg_cost)}"
                ),
                color=f"{Fore.GREEN}{Style.BRIGHT}",
            )
        )

    if be > 0 and p >= be and position.unrealized_pnl(p) >= 0:
        sell_qty = plan.recover_principal_qty
        msg = f"🎉【回本】价格已达持仓均价 {fmt_jpy(be)}，可考虑回收本金。"
        signals.append(
            Signal(
                key="breakeven_reached",
                title="回本提醒",
                console_msg=msg,
                card_template="orange",
                card_body=(
                    f"{msg}\n\n"
                    f"**当前价格：** {fmt_jpy(p)}\n"
                    f"**持仓：** {position.quantity:,.1f} XRP · 总成本 {fmt_jpy(position.total_cost_jpy)}\n"
                    f"**浮盈：** {fmt_jpy(position.unrealized_pnl(p))}\n"
                    f"**建议：** 卖出约 {sell_qty:,.1f} XRP 回收本金，其余零成本持有"
                ),
                color=f"{Fore.YELLOW}{Style.BRIGHT}",
            )
        )

    if be > 0 and p >= plan.milestone_80pct and p < be:
        msg = (
            f"📈【反弹进展】价格 {fmt_jpy(p)}，"
            f"距回本 {fmt_jpy(be)} 还差 {fmt_jpy(be - p)}。"
        )
        signals.append(
            Signal(
                key="recovery_progress",
                title="反弹进展",
                console_msg=msg,
                card_template="blue",
                card_body=(
                    f"{msg}\n\n"
                    f"**持仓浮亏：** {fmt_jpy(position.unrealized_pnl(p))}\n"
                    f"**30 日区间：** {fmt_jpy(snapshot.low_30d)} – {fmt_jpy(snapshot.high_30d)}\n"
                    f"**建议：** 持有观望，勿频繁操作"
                ),
                color=f"{Fore.CYAN}{Style.BRIGHT}",
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
    position: Position | None = None,
    available_jpy: float | None = None,
) -> dict[str, Any]:
    cd = cooldown or AlertCooldown(ALERT_COOLDOWN_SECONDS)
    pos = position or load_position()
    cash = load_available_jpy(available_jpy)
    snapshot, daily = build_snapshot()
    plan = build_recovery_plan(snapshot, pos, cash)
    advice = generate_trade_advice(snapshot, pos, plan)
    signals = detect_signals(snapshot, pos, plan)
    push_results = push_signals(signals, snapshot, cd)
    return {
        "snapshot": snapshot,
        "daily": daily,
        "position": pos,
        "available_jpy": cash,
        "recovery_plan": plan,
        "advice": advice,
        "chart_data": build_chart_data(daily, pos),
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
    position: Position,
    plan: RecoveryPlan,
    advice: list[TradeAdvice],
    signals: list[Signal],
    cooldown: AlertCooldown,
    available_jpy: float = 0.0,
) -> None:
    clear_screen()
    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    push_status = "飞书 Lark" if FEISHU_WEBHOOK_URL else "未配置"
    pnl = position.unrealized_pnl(snapshot.price)

    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  {PAIR_LABEL} 持仓回本监控{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"更新时间: {now}  |  数据源: {snapshot.data_source}")
    print(f"推送: {push_status}  |  冷却: 24h/信号  |  静默: {quiet_hours_label()}")
    print("-" * 54)
    print(f"当前价格: {Fore.WHITE}{Style.BRIGHT}{fmt_jpy(snapshot.price)}{Style.RESET_ALL}")
    print(f"日线 RSI: {Fore.MAGENTA}{snapshot.daily_rsi:.1f}{Style.RESET_ALL}")
    print("-" * 54)
    print("我的持仓:")
    print(f"  数量: {position.quantity:,.1f} XRP")
    print(f"  持仓均价: {Fore.BLUE}{fmt_jpy(plan.breakeven_price)}{Style.RESET_ALL}")
    print(f"  投入成本: {fmt_jpy(position.total_cost_jpy)}")
    print(f"  可支配日元: {fmt_jpy(available_jpy)}（单次建议加仓 {fmt_jpy(plan.dca_buy_jpy)}）")
    print(f"  市值: {fmt_jpy(position.market_value(snapshot.price))}")
    pnl_color = Fore.GREEN if pnl >= 0 else Fore.RED
    print(f"  浮盈浮亏: {pnl_color}{fmt_jpy(pnl)} ({position.pnl_pct(snapshot.price):+.1f}%){Style.RESET_ALL}")
    print(f"  距回本: {fmt_jpy(plan.distance_jpy)} ({plan.distance_pct:+.1f}%)")
    print("-" * 54)
    print("买卖建议:")
    print_advice(advice)
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
    position = load_position()
    available_jpy = load_available_jpy()
    print(f"正在启动 {PAIR_LABEL} 持仓回本监控...")

    while True:
        try:
            result = run_monitor_cycle(cooldown, position, available_jpy)
            print_dashboard(
                result["snapshot"],
                result["position"],
                result["recovery_plan"],
                result["advice"],
                result["signals"],
                result["cooldown"],
                result["available_jpy"],
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
