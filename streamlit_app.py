"""XRP/JPY Streamlit 监控面板 — 持仓回本分析与买卖建议。"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

import xrp_monitor as monitor


def apply_streamlit_secrets() -> None:
    """Streamlit Cloud 从 Secrets 注入环境变量。"""
    try:
        import os

        for key in (
            "FEISHU_WEBHOOK_URL",
            "FEISHU_SECRET",
            "APP_TIMEZONE",
            "HOLDINGS_XRP",
            "HOLDINGS_AVG_COST",
            "AVAILABLE_JPY",
        ):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
        monitor.reload_config()
    except (FileNotFoundError, AttributeError, RuntimeError):
        pass


ADVICE_STYLE = {
    "买入": "success",
    "卖出": "warning",
    "持有": "info",
    "观望": "secondary",
    "配置": "error",
}

SIGNAL_STYLE = {
    "extreme_oversold": ("success", "💡 极端超跌 · 加仓"),
    "breakeven_reached": ("warning", "🎉 回本提醒"),
    "recovery_progress": ("info", "📈 反弹进展"),
}


def init_session_state() -> None:
    if "alert_log" not in st.session_state:
        st.session_state.alert_log = []
    if "toast_keys" not in st.session_state:
        st.session_state.toast_keys = set()
    if "holdings_xrp" not in st.session_state:
        st.session_state.holdings_xrp = float(
            monitor.load_position().quantity
        )
    if "holdings_avg_cost" not in st.session_state:
        st.session_state.holdings_avg_cost = float(
            monitor.load_position().avg_cost_jpy
        )
    if "available_jpy" not in st.session_state:
        st.session_state.available_jpy = float(monitor.load_available_jpy())


def get_position() -> monitor.Position:
    return monitor.Position(
        quantity=st.session_state.holdings_xrp,
        avg_cost_jpy=st.session_state.holdings_avg_cost,
    )


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


def render_position(
    position: monitor.Position,
    snapshot: monitor.MarketSnapshot,
    available_jpy: float,
    plan: monitor.RecoveryPlan,
) -> None:
    st.subheader("我的持仓")
    pnl = position.unrealized_pnl(snapshot.price)
    cols = st.columns(4)
    cols[0].metric("持仓数量", f"{position.quantity:,.1f} XRP")
    cols[1].metric("持仓均价", monitor.fmt_jpy(position.avg_cost_jpy))
    cols[2].metric(
        "日元可支配资产",
        monitor.fmt_jpy(available_jpy),
        f"建议单次加仓 {monitor.fmt_jpy(plan.dca_buy_jpy)}",
    )
    cols[3].metric(
        "浮盈浮亏",
        monitor.fmt_jpy(pnl),
        f"{position.pnl_pct(snapshot.price):+.1f}%",
        delta_color="normal" if pnl >= 0 else "inverse",
    )


def render_recovery_plan(
    plan: monitor.RecoveryPlan,
    position: monitor.Position,
    snapshot: monitor.MarketSnapshot,
) -> None:
    st.subheader("回本分析")
    cols = st.columns(3)
    cols[0].metric(
        "回本目标价",
        monitor.fmt_jpy(plan.breakeven_price),
        f"还差 {monitor.fmt_jpy(abs(plan.distance_jpy))}" if plan.distance_jpy < 0 else "已回本",
        delta_color="inverse" if plan.distance_jpy < 0 else "normal",
    )
    cols[1].metric(
        f"补仓 {monitor.fmt_jpy(plan.dca_buy_jpy)} 后均价",
        monitor.fmt_jpy(plan.dca_breakeven_after),
        f"降 {monitor.fmt_jpy(plan.breakeven_price - plan.dca_breakeven_after)}",
    )
    cols[2].metric(
        "回收本金需卖",
        f"{plan.recover_principal_qty:,.1f} XRP",
        f"@{monitor.fmt_jpy(snapshot.price)}",
    )

    if plan.breakeven_price > 0:
        floor = snapshot.low_30d
        span = plan.breakeven_price - floor
        progress = 1.0 if span <= 0 else (snapshot.price - floor) / span
        progress = max(0.0, min(1.0, progress))
        st.progress(
            progress,
            text=(
                f"当前 {monitor.fmt_jpy(snapshot.price)} → "
                f"回本 {monitor.fmt_jpy(plan.breakeven_price)}"
            ),
        )

    st.caption(
        f"保守卖出参考 {monitor.fmt_jpy(plan.sell_target_conservative)} · "
        f"积极卖出参考 {monitor.fmt_jpy(plan.sell_target_aggressive)} · "
        f"30日区间 {monitor.fmt_jpy(snapshot.low_30d)}–{monitor.fmt_jpy(snapshot.high_30d)}"
    )


def render_trade_advice(advice: list[monitor.TradeAdvice]) -> None:
    st.subheader("买卖建议")
    for item in advice:
        style = ADVICE_STYLE.get(item.action, "info")
        label = f"**[{item.action}]** {item.strength} · {item.title}"
        body = f"{item.reason}\n\n{item.detail}"
        if style == "success":
            st.success(f"{label}\n\n{body}")
        elif style == "warning":
            st.warning(f"{label}\n\n{body}")
        elif style == "error":
            st.error(f"{label}\n\n{body}")
        else:
            st.info(f"{label}\n\n{body}")


def render_price_chart(chart_data) -> None:
    if chart_data is None or chart_data.empty:
        return
    st.subheader("价格走势")
    st.line_chart(chart_data, height=260)
    if "breakeven" in chart_data.columns:
        st.caption("虚线区域为持仓均价（回本价）")


def render_signals(
    signals: list[monitor.Signal], cooldown: monitor.AlertCooldown
) -> None:
    if not signals:
        return
    st.subheader("Lark 推送信号")
    for signal in signals:
        style, _ = SIGNAL_STYLE.get(signal.key, ("info", signal.title))
        remain = cooldown.remaining_hours(signal.key)
        suffix = f"（冷却中 {remain:.1f}h）" if remain > 0 else ""
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
                f"静默时段（{monitor.quiet_hours_label()}），暂不推送",
                "info",
            )


def render_monitor_panel(refresh_seconds: int) -> None:
    st.title("XRP/JPY 持仓回本监控")

    position = get_position()
    available_jpy = st.session_state.available_jpy

    try:
        cooldown = monitor.AlertCooldown(monitor.ALERT_COOLDOWN_SECONDS)
        result = monitor.run_monitor_cycle(cooldown, position, available_jpy)
    except Exception as exc:
        st.error(f"数据获取失败：{exc}")
        return

    snapshot: monitor.MarketSnapshot = result["snapshot"]
    plan: monitor.RecoveryPlan = result["recovery_plan"]
    advice: list[monitor.TradeAdvice] = result["advice"]
    signals: list[monitor.Signal] = result["signals"]
    push_results = result["push_results"]
    chart_data = result.get("chart_data")
    updated_at: datetime = result["updated_at"]

    st.caption(
        f"数据源: {snapshot.data_source} · RSI {snapshot.daily_rsi:.1f} · "
        f"自动刷新 {refresh_seconds}s · "
        f"推送冷却 {monitor.ALERT_COOLDOWN_SECONDS // 3600}h · "
        f"静默 {monitor.quiet_hours_label()}"
    )
    if monitor.is_quiet_hours():
        st.info(f"🌙 静默时段（{monitor.quiet_hours_label()}），Lark 暂不推送。")

    handle_push_results(push_results)

    for signal in signals:
        toast_key = f"ui_{signal.key}"
        if toast_key not in st.session_state.toast_keys:
            st.toast(signal.console_msg, icon="🔔")
            st.session_state.toast_keys.add(toast_key)

    top = st.columns(2)
    top[0].metric("当前价格", monitor.fmt_jpy(snapshot.price))
    top[1].metric("更新时间", updated_at.strftime("%H:%M:%S"))

    render_position(position, snapshot, available_jpy, plan)
    render_recovery_plan(plan, position, snapshot)
    render_trade_advice(advice)
    render_price_chart(chart_data)
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
        page_title="XRP/JPY 持仓监控",
        page_icon="📊",
        layout="wide",
    )
    apply_streamlit_secrets()
    init_session_state()

    with st.sidebar:
        st.header("我的持仓")
        st.session_state.holdings_xrp = st.number_input(
            "XRP 数量",
            min_value=0.0,
            value=st.session_state.holdings_xrp,
            step=100.0,
            format="%.1f",
        )
        st.session_state.holdings_avg_cost = st.number_input(
            "持仓均价（日元）",
            min_value=0.0,
            value=st.session_state.holdings_avg_cost,
            step=1.0,
            format="%.2f",
            help="用于计算回本价，可在交易所查看平均买入价",
        )
        st.session_state.available_jpy = st.number_input(
            "日元可支配资产",
            min_value=0.0,
            value=st.session_state.available_jpy,
            step=1000.0,
            format="%.0f",
            help="可用于买入 XRP 的日元，建议单次加仓约 1/3",
        )
        pos = get_position()
        if pos.quantity > 0 and pos.avg_cost_jpy > 0:
            st.caption(
                f"投入成本 {monitor.fmt_jpy(pos.total_cost_jpy)} · "
                f"单次建议加仓 {monitor.fmt_jpy(monitor.suggest_dca_jpy(st.session_state.available_jpy))}"
            )

        st.divider()
        st.header("设置")
        refresh_seconds = st.slider("自动刷新间隔（秒）", 30, 300, 60, step=30)
        lark_status = "已配置 ✅" if monitor.FEISHU_WEBHOOK_URL else "未配置 ❌"
        st.write(f"飞书 Lark：{lark_status}")
        st.write(f"推送冷却：{monitor.ALERT_COOLDOWN_SECONDS // 3600} 小时/信号")
        st.write(f"静默时段：{monitor.quiet_hours_label()}")
        st.write(
            f"当前：{'🌙 静默中' if monitor.is_quiet_hours() else '🔔 推送中'}"
        )
        if st.button("立即刷新", use_container_width=True):
            st.session_state.toast_keys.clear()
            st.rerun()

    @st.fragment(run_every=timedelta(seconds=refresh_seconds))
    def monitor_loop() -> None:
        render_monitor_panel(refresh_seconds)

    monitor_loop()


if __name__ == "__main__":
    main()
