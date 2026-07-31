"""XRP/JPY Streamlit 监控面板 — 120 万回本波段计划。"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

import xrp_monitor as monitor


def apply_streamlit_secrets() -> None:
    try:
        import os

        for key in (
            "FEISHU_WEBHOOK_URL",
            "FEISHU_SECRET",
            "APP_TIMEZONE",
            "HOLDINGS_XRP",
            "AVAILABLE_JPY",
            "TARGET_JPY",
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
    "extreme_oversold": ("success", "💡 极端超跌 · 低吸"),
    "target_reached": ("warning", "🎉 120万达成"),
}


def init_session_state() -> None:
    pf = monitor.load_portfolio()
    if "alert_log" not in st.session_state:
        st.session_state.alert_log = []
    if "toast_keys" not in st.session_state:
        st.session_state.toast_keys = set()
    if "holdings_xrp" not in st.session_state:
        st.session_state.holdings_xrp = pf.xrp_quantity
    if "available_jpy" not in st.session_state:
        st.session_state.available_jpy = pf.cash_jpy
    if "target_jpy" not in st.session_state:
        st.session_state.target_jpy = pf.target_jpy


def get_portfolio() -> monitor.Portfolio:
    return monitor.Portfolio(
        xrp_quantity=st.session_state.holdings_xrp,
        cash_jpy=st.session_state.available_jpy,
        target_jpy=st.session_state.target_jpy,
    )


def append_alert_log(message: str, level: str = "info") -> None:
    st.session_state.alert_log.insert(
        0,
        {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "message": message, "level": level},
    )
    st.session_state.alert_log = st.session_state.alert_log[:30]


def render_portfolio(
    portfolio: monitor.Portfolio,
    snapshot: monitor.MarketSnapshot,
    plan: monitor.RecoveryPlan,
) -> None:
    st.subheader("资产概况")
    pnl = portfolio.pnl(snapshot.price)
    cols = st.columns(4)
    cols[0].metric("XRP 持仓", f"{portfolio.xrp_quantity:,.0f} 枚", monitor.fmt_jpy(portfolio.xrp_value(snapshot.price)))
    cols[1].metric("日元现金", monitor.fmt_jpy(portfolio.cash_jpy))
    cols[2].metric("总资产", monitor.fmt_man(plan.total_assets), f"目标 {monitor.fmt_man(portfolio.target_jpy)}")
    cols[3].metric(
        "总盈亏",
        monitor.fmt_jpy(pnl),
        f"{portfolio.pnl_pct(snapshot.price):+.1f}%",
        delta_color="normal" if pnl >= 0 else "inverse",
    )


def render_recovery_plan(plan: monitor.RecoveryPlan) -> None:
    st.subheader("120 万回本进度")
    st.progress(
        plan.recovery_pct,
        text=f"当前 {monitor.fmt_man(plan.total_assets)} / 目标 {monitor.fmt_man(plan.target_jpy)} · 还差 {monitor.fmt_man(plan.recovery_gap)}",
    )
    cols = st.columns(3)
    cols[0].metric("下一目标", plan.next_milestone_label, f"还差 {monitor.fmt_man(plan.next_milestone - plan.total_assets)}")
    cols[1].metric("纯持有需涨至", monitor.fmt_jpy(plan.hold_only_price), "不含波段操作")
    cols[2].metric("单次波段预期", monitor.fmt_jpy(plan.swing_cycle_profit), f"基于 {monitor.fmt_jpy(plan.dca_buy_jpy)} 低吸 +12%")


def render_swing_plan(plan: monitor.RecoveryPlan) -> None:
    st.subheader("波段买卖计划")
    col_buy, col_sell = st.columns(2)

    with col_buy:
        st.markdown("**低吸（用现金）**")
        if plan.buy_steps:
            for step in plan.buy_steps:
                st.markdown(
                    f"- **{monitor.fmt_jpy(step.trigger_price)}** {step.trigger_label}\n"
                    f"  {step.amount_desc} → {step.result_desc}"
                )
        else:
            st.caption("当前现金不足，暂无低吸计划")

    with col_sell:
        st.markdown("**高抛（卖 XRP 换现金）**")
        if plan.sell_steps:
            for step in plan.sell_steps:
                st.markdown(
                    f"- **{monitor.fmt_jpy(step.trigger_price)}** {step.trigger_label}\n"
                    f"  {step.amount_desc} → {step.result_desc}"
                )
        else:
            st.caption("暂无高于当前价的高抛计划")


def render_trade_advice(advice: list[monitor.TradeAdvice]) -> None:
    st.subheader("当前建议")
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
    st.line_chart(chart_data, height=240)


def render_signals(signals: list[monitor.Signal], cooldown: monitor.AlertCooldown) -> None:
    if not signals:
        return
    st.subheader("Lark 推送")
    for signal in signals:
        style, _ = SIGNAL_STYLE.get(signal.key, ("info", signal.title))
        remain = cooldown.remaining_hours(signal.key)
        suffix = f"（冷却 {remain:.1f}h）" if remain > 0 else ""
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
            append_alert_log(f"静默时段（{monitor.quiet_hours_label()}），暂不推送", "info")


def render_monitor_panel(refresh_seconds: int) -> None:
    st.title("XRP/JPY · 120 万回本波段计划")

    portfolio = get_portfolio()

    try:
        cooldown = monitor.AlertCooldown(monitor.ALERT_COOLDOWN_SECONDS)
        result = monitor.run_monitor_cycle(cooldown, portfolio)
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
        f"刷新 {refresh_seconds}s · 静默 {monitor.quiet_hours_label()}"
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

    render_portfolio(portfolio, snapshot, plan)
    render_recovery_plan(plan)
    render_trade_advice(advice)
    render_swing_plan(plan)
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
    st.set_page_config(page_title="XRP/JPY 120万回本", page_icon="📊", layout="wide")
    apply_streamlit_secrets()
    init_session_state()

    with st.sidebar:
        st.header("我的资产")
        st.session_state.holdings_xrp = st.number_input(
            "XRP 数量", min_value=0.0, value=st.session_state.holdings_xrp, step=1.0, format="%.0f"
        )
        st.session_state.available_jpy = st.number_input(
            "日元可支配资产",
            min_value=0.0,
            value=st.session_state.available_jpy,
            step=1000.0,
            format="%.0f",
            help="可用于买入 XRP 的现金",
        )
        st.session_state.target_jpy = st.number_input(
            "回本目标（日元）",
            min_value=0.0,
            value=st.session_state.target_jpy,
            step=10000.0,
            format="%.0f",
            help="原始投入总额，如 120 万",
        )
        pf = get_portfolio()
        st.caption(
            f"单次低吸建议 {monitor.fmt_jpy(monitor.suggest_dca_jpy(pf.cash_jpy))}（现金 1/3）"
        )

        st.divider()
        st.header("设置")
        refresh_seconds = st.slider("自动刷新间隔（秒）", 30, 300, 60, step=30)
        st.write(f"飞书 Lark：{'已配置 ✅' if monitor.FEISHU_WEBHOOK_URL else '未配置 ❌'}")
        st.write(f"静默：{monitor.quiet_hours_label()}")
        if st.button("立即刷新", use_container_width=True):
            st.session_state.toast_keys.clear()
            st.rerun()

    @st.fragment(run_every=timedelta(seconds=refresh_seconds))
    def monitor_loop() -> None:
        render_monitor_panel(refresh_seconds)

    monitor_loop()


if __name__ == "__main__":
    main()
