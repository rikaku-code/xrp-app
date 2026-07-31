"""XRP/JPY Streamlit 监控面板 — 自动刷新 + Lark 买卖信号推送。"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

import xrp_monitor as monitor


def apply_streamlit_secrets() -> None:
    """Streamlit Cloud 从 Secrets 注入环境变量。"""
    try:
        for key in ("FEISHU_WEBHOOK_URL", "FEISHU_SECRET", "APP_TIMEZONE"):
            if key in st.secrets:
                import os
                os.environ[key] = str(st.secrets[key])
        monitor.reload_config()
    except (FileNotFoundError, AttributeError, RuntimeError):
        pass

SIGNAL_STYLE = {
    "extreme_oversold": ("success", "💡 极端超跌买点"),
    "rebound_190": ("info", "📈 反弹阶梯 ¥190"),
    "breakeven_210": ("warning", "🎉 回本提醒 ¥210"),
}


def init_session_state() -> None:
    if "alert_log" not in st.session_state:
        st.session_state.alert_log = []
    if "toast_keys" not in st.session_state:
        st.session_state.toast_keys = set()


def append_alert_log(message: str, level: str = "info") -> None:
    st.session_state.alert_log.insert(
        0,
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": message,
            "level": level,
        },
    )
    st.session_state.alert_log = st.session_state.alert_log[:30]


def render_technical_indicators(snapshot: monitor.MarketSnapshot) -> None:
    st.subheader("技术指标")
    row1 = st.columns(4)
    row1[0].metric(
        f"RSI({monitor.DAILY_RSI_PERIOD})",
        f"{snapshot.daily_rsi:.1f}",
        monitor.rsi_zone(snapshot.daily_rsi),
    )
    row1[1].metric("MA20", f"¥{snapshot.ma20:,.2f}")
    row1[2].metric("MA50", f"¥{snapshot.ma50:,.2f}")
    row1[3].metric("均线趋势", monitor.ma_trend(snapshot.price, snapshot.ma20, snapshot.ma50))

    row2 = st.columns(4)
    row2[0].metric("MACD", f"{snapshot.macd:+.3f}")
    row2[1].metric("MACD 信号", f"{snapshot.macd_signal:+.3f}")
    row2[2].metric("MACD 柱", f"{snapshot.macd_hist:+.3f}", monitor.macd_trend(snapshot.macd_hist))
    row2[3].metric(
        "布林带",
        monitor.bb_position(snapshot.price, snapshot.bb_upper, snapshot.bb_lower),
        delta=f"¥{snapshot.bb_lower:,.0f}–¥{snapshot.bb_upper:,.0f}",
    )

    row3 = st.columns(3)
    row3[0].metric("30日最低", f"¥{snapshot.low_30d:,.2f}")
    row3[1].metric("30日最高", f"¥{snapshot.high_30d:,.2f}")
    dist_cost = snapshot.price - monitor.MY_COST_PRICE
    row3[2].metric(
        "距成本价",
        f"{'+' if dist_cost >= 0 else ''}{dist_cost:,.2f} JPY",
        delta=f"成本 ¥{monitor.MY_COST_PRICE}",
        delta_color="normal" if dist_cost >= 0 else "inverse",
    )


def render_price_chart(chart_data) -> None:
    if chart_data is None or chart_data.empty:
        return
    st.subheader("价格与均线（近 60 日）")
    st.line_chart(chart_data, height=280)


def render_signal_conditions(snapshot: monitor.MarketSnapshot) -> None:
    st.subheader("触发条件检查")
    cols = st.columns(3)

    extreme_ok = (
        snapshot.daily_rsi < monitor.DAILY_RSI_EXTREME
        and monitor.at_30d_low(snapshot)
    )
    cols[0].metric(
        "极端超跌",
        "已满足" if extreme_ok else "未满足",
        delta=f"RSI {snapshot.daily_rsi:.1f} / 需 < {monitor.DAILY_RSI_EXTREME}",
        delta_color="normal" if extreme_ok else "off",
    )

    rebound_ok = snapshot.price >= monitor.REBOUND_PRICE
    cols[1].metric(
        "反弹 ¥190",
        "已满足" if rebound_ok else "未满足",
        delta=f"还差 ¥{max(0, monitor.REBOUND_PRICE - snapshot.price):.2f}",
        delta_color="normal" if rebound_ok else "off",
    )

    breakeven_ok = snapshot.price >= monitor.MY_COST_PRICE
    cols[2].metric(
        "回本 ¥210",
        "已满足" if breakeven_ok else "未满足",
        delta=f"还差 ¥{max(0, monitor.MY_COST_PRICE - snapshot.price):.2f}",
        delta_color="normal" if breakeven_ok else "off",
    )


def render_price_progress(snapshot: monitor.MarketSnapshot) -> None:
    st.subheader("解套进度")
    floor_price = snapshot.low_30d
    span = monitor.MY_COST_PRICE - floor_price
    progress = 1.0 if span <= 0 else (snapshot.price - floor_price) / span
    progress = max(0.0, min(1.0, progress))
    st.progress(
        progress,
        text=f"当前 ¥{snapshot.price:,.2f} → 目标成本 ¥{monitor.MY_COST_PRICE}",
    )


def render_signals(
    signals: list[monitor.Signal], cooldown: monitor.AlertCooldown
) -> None:
    st.subheader("买卖信号")
    if not signals:
        st.info("当前暂无触发信号，程序持续监控中。")
        return

    for signal in signals:
        style, _ = SIGNAL_STYLE.get(signal.key, ("info", signal.title))
        remain = cooldown.remaining_hours(signal.key)
        suffix = f"（Lark 冷却中，剩余 {remain:.1f} 小时）" if remain > 0 else ""
        message = f"{signal.console_msg}{suffix}"
        if style == "success":
            st.success(message)
        elif style == "warning":
            st.warning(message)
        else:
            st.info(message)


def handle_push_results(push_results: list[dict]) -> None:
    for item in push_results:
        title = item["title"]
        status = item["status"]
        key = item["key"]
        if status == "sent":
            msg = f"Lark 已推送：{title}"
            append_alert_log(msg, "success")
            if key not in st.session_state.toast_keys:
                st.toast(msg, icon="📲")
                st.session_state.toast_keys.add(key)
        elif status == "failed":
            msg = f"Lark 推送失败：{title} — {item.get('error', '未知错误')}"
            append_alert_log(msg, "error")
            st.error(msg)
        elif status == "cooldown":
            st.session_state.toast_keys.discard(key)
        elif status == "quiet_hours":
            append_alert_log(
                f"静默时段（{monitor.quiet_hours_label()}），暂不推送 Lark",
                "info",
            )


def render_monitor_panel(refresh_seconds: int) -> None:
    st.title("XRP/JPY 长期持仓监控")

    try:
        cooldown = monitor.AlertCooldown(monitor.ALERT_COOLDOWN_SECONDS)
        result = monitor.run_monitor_cycle(cooldown)
    except Exception as exc:
        st.error(f"数据获取失败：{exc}")
        return

    snapshot: monitor.MarketSnapshot = result["snapshot"]
    signals: list[monitor.Signal] = result["signals"]
    push_results = result["push_results"]
    chart_data = result.get("chart_data")
    updated_at: datetime = result["updated_at"]

    st.caption(
        f"数据源: {snapshot.data_source} · 飞书 Lark 推送 · "
        f"自动刷新 {refresh_seconds} 秒 · "
        f"每信号 {monitor.ALERT_COOLDOWN_SECONDS // 3600} 小时冷却 · "
        f"静默 {monitor.quiet_hours_label()}"
    )
    if monitor.is_quiet_hours():
        st.info(f"🌙 当前为静默时段（{monitor.quiet_hours_label()}），Lark 暂不推送。")

    handle_push_results(push_results)

    for signal in signals:
        toast_key = f"ui_{signal.key}"
        if toast_key not in st.session_state.toast_keys:
            st.toast(signal.console_msg, icon="🔔")
            st.session_state.toast_keys.add(toast_key)

    top = st.columns(4)
    top[0].metric("当前价格", f"¥{snapshot.price:,.2f}")
    top[1].metric(
        "日线 RSI",
        f"{snapshot.daily_rsi:.2f}",
        monitor.rsi_zone(snapshot.daily_rsi),
    )
    top[2].metric("30日区间", f"¥{snapshot.low_30d:,.0f}–¥{snapshot.high_30d:,.0f}")
    top[3].metric("更新时间", updated_at.strftime("%H:%M:%S"))

    render_technical_indicators(snapshot)
    render_price_chart(chart_data)
    render_price_progress(snapshot)
    render_signal_conditions(snapshot)
    render_signals(signals, cooldown)

    with st.expander("推送记录", expanded=False):
        if st.session_state.alert_log:
            for entry in st.session_state.alert_log:
                if entry["level"] == "success":
                    st.success(f"{entry['time']} · {entry['message']}")
                elif entry["level"] == "error":
                    st.error(f"{entry['time']} · {entry['message']}")
                else:
                    st.write(f"{entry['time']} · {entry['message']}")
        else:
            st.write("暂无推送记录")


def main() -> None:
    st.set_page_config(
        page_title="XRP/JPY 监控",
        page_icon="📊",
        layout="wide",
    )
    apply_streamlit_secrets()
    init_session_state()

    with st.sidebar:
        st.header("设置")
        refresh_seconds = st.slider("自动刷新间隔（秒）", 30, 300, 60, step=30)
        lark_status = "已配置 ✅" if monitor.FEISHU_WEBHOOK_URL else "未配置 ❌"
        st.write(f"飞书 Lark：{lark_status}")
        st.write(f"成本价：¥{monitor.MY_COST_PRICE}")
        st.write(f"反弹阶梯：¥{monitor.REBOUND_PRICE}")
        st.write(f"推送冷却：{monitor.ALERT_COOLDOWN_SECONDS // 3600} 小时/信号")
        st.write(f"静默时段：{monitor.quiet_hours_label()}（不推 Lark）")
        st.write(
            f"当前状态：{'🌙 静默中' if monitor.is_quiet_hours() else '🔔 推送中'}"
        )
        if st.button("立即刷新", use_container_width=True):
            st.session_state.toast_keys.clear()
            st.rerun()
        st.divider()
        st.markdown(
            "**启动命令**\n\n"
            "```bash\n"
            "cd xrp_monitor\n"
            "pip install -r requirements.txt\n"
            "streamlit run streamlit_app.py\n"
            "```"
        )

    @st.fragment(run_every=timedelta(seconds=refresh_seconds))
    def monitor_loop() -> None:
        render_monitor_panel(refresh_seconds)

    monitor_loop()


if __name__ == "__main__":
    main()
