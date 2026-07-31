#!/usr/bin/env python3
"""XRP/JPY 长期持仓监控 — 极端低吸与解套阶梯提醒。"""

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
SYMBOL = "XRPJPY"
PAIR_LABEL = "XRP/JPY"
REFRESH_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 24 * 60 * 60
QUIET_HOUR_START = 0
QUIET_HOUR_END = 8

# ── 个人成本与解套阶梯 ──────────────────────────────────────
MY_COST_PRICE = 210
REBOUND_PRICE = 190

# ── 极端超跌条件 ────────────────────────────────────────────
DAILY_RSI_PERIOD = 14
DAILY_RSI_EXTREME = 25
LOW_LOOKBACK_DAYS = 30
LOW_TOUCH_TOLERANCE = 0.01
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Tokyo")

FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")


@dataclass(frozen=True)
class MarketSnapshot:
    price: float
    daily_rsi: float
    low_30d: float
    data_source: str


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
    """凌晨 0:00–8:00 为静默时段，不推送 Lark（按 APP_TIMEZONE）。"""
    hour = (now or now_local()).hour
    return QUIET_HOUR_START <= hour < QUIET_HOUR_END


def quiet_hours_label() -> str:
    return f"{QUIET_HOUR_START:02d}:00–{QUIET_HOUR_END:02d}:00"


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def fetch_binance_klines(interval: str, limit: int) -> pd.DataFrame:
    params = {"symbol": SYMBOL, "interval": interval, "limit": limit}
    response = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
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


def build_snapshot() -> MarketSnapshot:
    intraday = fetch_binance_klines("15m", 5)
    daily = fetch_binance_klines("1d", max(LOW_LOOKBACK_DAYS + DAILY_RSI_PERIOD, 60))

    price = float(intraday["close"].iloc[-1])
    rsi_series = ta.momentum.RSIIndicator(
        close=daily["close"], window=DAILY_RSI_PERIOD
    ).rsi()
    daily_rsi = float(rsi_series.iloc[-1])
    low_30d = float(daily["low"].tail(LOW_LOOKBACK_DAYS).min())

    return MarketSnapshot(
        price=price,
        daily_rsi=daily_rsi,
        low_30d=low_30d,
        data_source="Binance",
    )


def at_30d_low(snapshot: MarketSnapshot) -> bool:
    threshold = snapshot.low_30d * (1 + LOW_TOUCH_TOLERANCE)
    return snapshot.price <= threshold


def detect_signals(snapshot: MarketSnapshot) -> list[Signal]:
    signals: list[Signal] = []
    p = snapshot.price
    fmt = lambda v: f"¥{v:,.2f}"

    if snapshot.daily_rsi < DAILY_RSI_EXTREME and at_30d_low(snapshot):
        msg = (
            "💡【极端超跌预警】XRP 达到极低位，"
            "可用 1/3 现金（约 3 万日元）挂低价单买入，增加筹码数量。"
        )
        signals.append(
            Signal(
                key="extreme_oversold",
                title="极端超跌买点",
                console_msg=msg,
                card_template="green",
                card_body=(
                    f"{msg}\n\n"
                    f"**当前价格：** {fmt(p)}\n"
                    f"**日线 RSI({DAILY_RSI_PERIOD})：** {snapshot.daily_rsi:.2f}\n"
                    f"**近 {LOW_LOOKBACK_DAYS} 日最低：** {fmt(snapshot.low_30d)}\n"
                    f"**我的成本价：** {fmt(MY_COST_PRICE)}"
                ),
                color=f"{Fore.GREEN}{Style.BRIGHT}",
            )
        )

    if p >= REBOUND_PRICE:
        msg = "📈 反弹预警：已恢复至 190 JPY，请保持耐心。"
        signals.append(
            Signal(
                key="rebound_190",
                title="反弹阶梯提醒",
                console_msg=msg,
                card_template="blue",
                card_body=(
                    f"{msg}\n\n"
                    f"**当前价格：** {fmt(p)}\n"
                    f"**反弹目标：** {fmt(REBOUND_PRICE)}\n"
                    f"**我的成本价：** {fmt(MY_COST_PRICE)}\n"
                    f"**日线 RSI：** {snapshot.daily_rsi:.2f}"
                ),
                color=f"{Fore.CYAN}{Style.BRIGHT}",
            )
        )

    if p >= MY_COST_PRICE:
        msg = f"🎉 回本预警：价格已到达成本价 {MY_COST_PRICE} JPY！"
        signals.append(
            Signal(
                key="breakeven_210",
                title="回本提醒",
                console_msg=msg,
                card_template="orange",
                card_body=(
                    f"{msg}\n\n"
                    f"**当前价格：** {fmt(p)}\n"
                    f"**成本价：** {fmt(MY_COST_PRICE)}\n"
                    f"**日线 RSI：** {snapshot.daily_rsi:.2f}\n"
                    f"**近 {LOW_LOOKBACK_DAYS} 日最低：** {fmt(snapshot.low_30d)}"
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
                {
                    "key": signal.key,
                    "title": signal.title,
                    "status": "quiet_hours",
                }
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
                f"（今日已通知，24 小时内不重复）{Style.RESET_ALL}"
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
) -> dict[str, Any]:
    """执行一次监控周期，供 CLI 与 Streamlit 共用。"""
    cd = cooldown or AlertCooldown(ALERT_COOLDOWN_SECONDS)
    snapshot = build_snapshot()
    signals = detect_signals(snapshot)
    push_results = push_signals(signals, snapshot, cd)
    return {
        "snapshot": snapshot,
        "signals": signals,
        "push_results": push_results,
        "cooldown": cd,
        "updated_at": now_local(),
    }


def print_signals(signals: list[Signal], cooldown: AlertCooldown) -> None:
    if not signals:
        print(f"{Fore.WHITE}暂无触发信号{Style.RESET_ALL}")
        return
    for signal in signals:
        remain = cooldown.remaining_hours(signal.key)
        suffix = f"（今日已推送，剩余冷却 {remain:.1f} 小时）" if remain > 0 else ""
        print(f"{signal.color}{signal.console_msg}{suffix}{Style.RESET_ALL}")


def print_dashboard(
    snapshot: MarketSnapshot,
    signals: list[Signal],
    cooldown: AlertCooldown,
) -> None:
    clear_screen()
    now = now_local().strftime("%Y-%m-%d %H:%M:%S")
    push_status = "飞书 Lark" if FEISHU_WEBHOOK_URL else "未配置"

    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}  {PAIR_LABEL} 长期持仓监控{Style.RESET_ALL}")
    print(f"{Fore.GREEN}{Style.BRIGHT}{'=' * 54}{Style.RESET_ALL}")
    print(f"更新时间: {now}")
    print(f"交易对: {SYMBOL}  |  数据源: {snapshot.data_source}")
    print(f"刷新间隔: {REFRESH_SECONDS} 秒  |  推送冷却: 每信号 24 小时/次")
    quiet = "是（静默中）" if is_quiet_hours() else f"否（{quiet_hours_label()} 不推送）"
    print(f"静默时段: {quiet_hours_label()}  |  当前: {quiet}")
    print(f"手机推送: {push_status}")
    print("-" * 54)
    print(f"当前价格: {Fore.WHITE}{Style.BRIGHT}¥{snapshot.price:,.2f}{Style.RESET_ALL}")
    print(
        f"日线 RSI({DAILY_RSI_PERIOD}): "
        f"{Fore.MAGENTA}{Style.BRIGHT}{snapshot.daily_rsi:.2f}{Style.RESET_ALL}"
    )
    print(
        f"近{LOW_LOOKBACK_DAYS}日最低: "
        f"{Fore.YELLOW}{Style.BRIGHT}¥{snapshot.low_30d:,.2f}{Style.RESET_ALL}"
    )
    print(f"反弹阶梯: {Fore.CYAN}¥{REBOUND_PRICE}{Style.RESET_ALL}")
    print(f"我的成本: {Fore.BLUE}¥{MY_COST_PRICE}{Style.RESET_ALL}")
    print("-" * 54)
    print("信号状态:")
    print_signals(signals, cooldown)
    print(f"\n{Fore.WHITE}按 Ctrl+C 退出程序{Style.RESET_ALL}")


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
    print(f"正在启动 {PAIR_LABEL} 长期持仓监控...")

    while True:
        try:
            result = run_monitor_cycle(cooldown)
            print_dashboard(
                result["snapshot"], result["signals"], result["cooldown"]
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
