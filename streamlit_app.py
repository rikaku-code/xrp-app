"""XRP/JPY Streamlit 监控面板 — 回本波段计划。"""

from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

import xrp_monitor as monitor

ACTION_CSS = """
<style>
.action-hero {
    border-radius: 14px;
    padding: 1.25rem 1.5rem 1.1rem;
    margin: 0.75rem 0 1.25rem;
    border: 2px solid;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
}
.action-hero.wait {
    background: linear-gradient(135deg, #1a2a40 0%, #243b55 100%);
    border-color: #5eb3ff;
}
.action-hero.buy {
    background: linear-gradient(135deg, #0f3320 0%, #1a4d32 100%);
    border-color: #3ddc84;
}
.action-hero.sell {
    background: linear-gradient(135deg, #3d2e0a 0%, #5c4512 100%);
    border-color: #ffc107;
}
.action-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
}
.action-badge {
    font-size: 2rem;
    font-weight: 900;
    letter-spacing: 0.08em;
    line-height: 1.1;
}
.action-badge.wait { color: #5eb3ff; }
.action-badge.buy { color: #3ddc84; }
.action-badge.sell { color: #ffc107; }
.action-time {
    font-size: 0.95rem;
    opacity: 0.85;
    font-weight: 600;
}
.action-title {
    font-size: 1.25rem;
    font-weight: 700;
    margin-bottom: 0.35rem;
}
.action-reason {
    font-size: 0.95rem;
    opacity: 0.9;
    margin-bottom: 1rem;
}
.action-metrics {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 0.75rem;
}
@media (max-width: 768px) {
    .action-metrics { grid-template-columns: repeat(2, 1fr); }
}
.action-metric {
    background: rgba(255, 255, 255, 0.1);
    border-radius: 10px;
    padding: 0.85rem 1rem;
    text-align: center;
    border: 1px solid rgba(255, 255, 255, 0.15);
}
.action-metric-label {
    font-size: 0.8rem;
    opacity: 0.75;
    margin-bottom: 0.25rem;
    font-weight: 600;
}
.action-metric-value {
    font-size: 1.35rem;
    font-weight: 800;
    line-height: 1.2;
}
.action-metric-value.dim {
    opacity: 0.45;
    font-weight: 600;
}
.action-next {
    margin-top: 0.85rem;
    font-size: 0.88rem;
    opacity: 0.8;
    padding-top: 0.65rem;
    border-top: 1px solid rgba(255, 255, 255, 0.12);
}
.plan-step-card {
    border-radius: 10px;
    padding: 0.75rem 1rem;
    margin-bottom: 0.6rem;
    border-left: 4px solid;
}
.plan-step-card.buy-step {
    background: rgba(61, 220, 132, 0.08);
    border-color: #3ddc84;
}
.plan-step-card.sell-step {
    background: rgba(255, 193, 7, 0.08);
    border-color: #ffc107;
}
.plan-step-price {
    font-size: 1.1rem;
    font-weight: 800;
}
.plan-step-qty {
    font-size: 1rem;
    font-weight: 700;
    margin-top: 0.2rem;
}
</style>
"""

ACTION_BADGE = {
    "等待": ("wait", "⏸ 等待"),
    "买入": ("buy", "🟢 买入"),
    "卖出": ("sell", "🟡 卖出"),
}

SIGNAL_STYLE = {
    "tech_buy": ("success", "💡 RSI 买点"),
    "tech_sell": ("warning", "📤 RSI 卖点"),
    "target_reached": ("warning", "🎉 目标达成"),
}

ADVICE_STYLE = {
    "买入": "success",
    "卖出": "warning",
    "持有": "info",
    "等待": "secondary",
    "观望": "secondary",
    "配置": "error",
}


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


def _fmt_qty(value: float, unit: str) -> str:
    if value <= 0:
        return "—"
    if unit == "XRP":
        return f"{value:,.1f} {unit}"
    return f"{monitor.fmt_jpy(value)}"


def render_current_action(
    action: monitor.CurrentAction,
    updated_at: datetime,
    snapshot: monitor.MarketSnapshot,
) -> None:
    css_class, badge_text = ACTION_BADGE.get(action.action, ("wait", "⏸ 等待"))
    buy_jpy = _fmt_qty(action.buy_jpy, "JPY")
    buy_xrp = _fmt_qty(action.buy_xrp, "XRP")
    sell_xrp = _fmt_qty(action.sell_xrp, "XRP")
    sell_jpy = _fmt_qty(action.sell_jpy, "JPY")

    buy_cls = "" if action.buy_jpy > 0 else " dim"
    buy_xrp_cls = "" if action.buy_xrp > 0 else " dim"
    sell_xrp_cls = "" if action.sell_xrp > 0 else " dim"
    sell_jpy_cls = "" if action.sell_jpy > 0 else " dim"

    next_bits = []
    if action.next_buy_price:
        next_bits.append(f"下次低吸 <b>{monitor.fmt_jpy(action.next_buy_price)}</b>")
    if action.next_sell_price:
        next_bits.append(f"下次高抛 <b>{monitor.fmt_jpy(action.next_sell_price)}</b>")
    next_html = " · ".join(next_bits) if next_bits else "详见下方波段计划表"

    trigger = ""
    if action.trigger_price:
        trigger = f" · 参考价 {monitor.fmt_jpy(action.trigger_price)}"
    elif action.action == "等待":
        trigger = f" · 现价 {monitor.fmt_jpy(snapshot.price)}"

    st.markdown(
        f"""
<div class="action-hero {css_class}">
  <div class="action-header">
    <div class="action-badge {css_class}">{badge_text}</div>
    <div class="action-time">🕐 {updated_at.strftime("%Y-%m-%d %H:%M:%S")} 刷新</div>
  </div>
  <div class="action-title">{action.title}</div>
  <div class="action-reason">{action.reason}{trigger}</div>
  <div class="action-metrics">
    <div class="action-metric">
      <div class="action-metric-label">买入金额</div>
      <div class="action-metric-value{buy_cls}">{buy_jpy}</div>
    </div>
    <div class="action-metric">
      <div class="action-metric-label">买入数量</div>
      <div class="action-metric-value{buy_xrp_cls}">{buy_xrp}</div>
    </div>
    <div class="action-metric">
      <div class="action-metric-label">卖出数量</div>
      <div class="action-metric-value{sell_xrp_cls}">{sell_xrp}</div>
    </div>
    <div class="action-metric">
      <div class="action-metric-label">卖出金额</div>
      <div class="action-metric-value{sell_jpy_cls}">{sell_jpy}</div>
    </div>
  </div>
  <div class="action-next">📍 {next_html}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


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
    cols[2].metric("总资产", monitor.fmt_jpy(plan.total_assets), f"目标 {monitor.fmt_jpy(portfolio.target_jpy)}")
    cols[3].metric(
        "总盈亏",
        monitor.fmt_jpy(pnl),
        f"{portfolio.pnl_pct(snapshot.price):+.1f}%",
        delta_color="normal" if pnl >= 0 else "inverse",
    )


def render_cycle_context(cycle: monitor.CycleContext, snapshot: monitor.MarketSnapshot) -> None:
    st.subheader("4 年周期参考（不单独触发买卖）")
    st.progress(
        cycle.position_pct / 100,
        text=(
            f"4年位置 {cycle.position_pct:.0f}% · {cycle.phase} · "
            f"区间 {monitor.fmt_jpy(cycle.range_low)} – {monitor.fmt_jpy(cycle.range_high)}"
        ),
    )
    cols = st.columns(4)
    cols[0].metric("周期阶段", cycle.phase, cycle.halving_label)
    cols[1].metric("自高点回落", f"{cycle.drawdown_pct:.0f}%", f"高点 {monitor.fmt_jpy(cycle.range_high)}")
    cols[2].metric(f"{cycle.month}月季节", cycle.month_strength, f"月均 {cycle.month_return_pct:+.1f}%")
    cols[3].metric("样本天数", f"{cycle.data_days} 天", cycle.month_history[:20] + "…")
    st.caption(f"{cycle.phase_detail} · 以下价位仅供挂单参考，须等 RSI 等技术信号确认后再操作")


def render_technical_context(
    tech: monitor.TechnicalContext,
    snapshot: monitor.MarketSnapshot,
) -> None:
    st.subheader("技术指标 · 操作依据")
    cols = st.columns(5)
    cols[0].metric("RSI(14)", f"{tech.rsi:.0f}", tech.rsi_zone)
    stoch_label = "金叉" if tech.stoch_golden else ("死叉" if tech.stoch_dead else "—")
    cols[1].metric("Stoch %K", f"{tech.stoch_k:.0f}", f"D={tech.stoch_d:.0f} · {stoch_label}")
    macd_label = "上穿零轴" if tech.macd_hist_bullish else ("下穿零轴" if tech.macd_hist_bearish else "—")
    cols[2].metric("MACD 柱", f"{tech.macd_hist:+.2f}", macd_label)
    cols[3].metric("MA20", monitor.fmt_jpy(snapshot.ma20))
    cols[4].metric("MA50", monitor.fmt_jpy(snapshot.ma50))
    if tech.buy_triggered:
        st.success(f"买入条件：**已满足** — {tech.buy_reason}")
    else:
        st.info(f"买入条件：未满足 — {tech.buy_reason}")
    if tech.sell_triggered:
        st.warning(f"卖出条件：**已满足** — {tech.sell_reason}")
    else:
        st.info(f"卖出条件：未满足 — {tech.sell_reason}")
    if tech.stoch_golden or tech.stoch_dead:
        cross = "ゴールデンクロス（K上穿D）" if tech.stoch_golden else "デッドクロス（K下穿D）"
        st.caption(
            f"Stoch：{cross} · 仅在 K≤{monitor.STOCH_OVERSOLD} 买 / K≥{monitor.STOCH_OVERBOUGHT} 卖时触发"
        )
    if tech.macd_hist_bullish or tech.macd_hist_bearish:
        cross = "柱上穿零轴（強気）" if tech.macd_hist_bullish else "柱下穿零轴（弱気）"
        st.caption(
            f"MACD：{cross} · 仅在 RSI<{monitor.RSI_OVERSOLD} 买 / RSI≥65 卖时触发"
        )
    st.caption(
        f"布林带 {monitor.fmt_jpy(snapshot.bb_lower)} – {monitor.fmt_jpy(snapshot.bb_upper)}"
    )


def render_recovery_plan(plan: monitor.RecoveryPlan) -> None:
    st.subheader("回本进度")
    st.progress(
        plan.recovery_pct,
        text=f"当前 {monitor.fmt_jpy(plan.total_assets)} / 目标 {monitor.fmt_jpy(plan.target_jpy)} · 还差 {monitor.fmt_jpy(plan.recovery_gap)}",
    )
    cols = st.columns(3)
    cols[0].metric("下一目标", plan.next_milestone_label, f"还差 {monitor.fmt_jpy(plan.next_milestone - plan.total_assets)}")
    cols[1].metric("纯持有需涨至", monitor.fmt_jpy(plan.hold_only_price), "不含波段操作")
    cols[2].metric("单次波段预期", monitor.fmt_jpy(plan.swing_cycle_profit), f"基于 {monitor.fmt_jpy(plan.dca_buy_jpy)} 低吸 +12%")


def render_swing_plan(plan: monitor.RecoveryPlan, current_price: float) -> None:
    st.subheader("周期参考价位表（需 RSI 确认）")
    st.caption("表中为历史统计的挂单参考位，**不等于立即买入/卖出**。")
    col_buy, col_sell = st.columns(2)

    with col_buy:
        st.markdown("#### 📋 参考低吸位")
        if plan.buy_steps:
            for step in plan.buy_steps:
                near = abs(current_price - step.trigger_price) / step.trigger_price < 0.03
                marker = "📍 " if near else ""
                st.markdown(
                    f"""
<div class="plan-step-card buy-step">
  <div class="plan-step-price">{marker}{monitor.fmt_jpy(step.trigger_price)} · {step.trigger_label}</div>
  <div class="plan-step-qty">{step.amount_desc} · {step.amount_xrp:,.1f} XRP</div>
  <div style="font-size:0.85rem;opacity:0.85;margin-top:0.3rem;">{step.result_desc}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无参考低吸位")

    with col_sell:
        st.markdown("#### 📋 参考高抛位")
        if plan.sell_steps:
            for step in plan.sell_steps:
                near = abs(current_price - step.trigger_price) / step.trigger_price < 0.03
                marker = "📍 " if near else ""
                st.markdown(
                    f"""
<div class="plan-step-card sell-step">
  <div class="plan-step-price">{marker}{monitor.fmt_jpy(step.trigger_price)} · {step.trigger_label}</div>
  <div class="plan-step-qty">{step.amount_desc} · {monitor.fmt_jpy(step.amount_jpy)}</div>
  <div style="font-size:0.85rem;opacity:0.85;margin-top:0.3rem;">{step.result_desc}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无参考高抛位")


def render_trade_advice(advice: list[monitor.TradeAdvice]) -> None:
    st.subheader("详细说明")
    for item in advice:
        if item.action == "持有" and item.strength in ("参考", "技术", "周期"):
            continue
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
    if "p25" in chart_data.columns:
        st.caption("参考线：4年 25% / 75% 分位（周期低吸/高抛带）")


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


def _resolve_cycle(result: dict) -> monitor.CycleContext | None:
    cycle = result.get("cycle")
    if cycle is not None:
        return cycle
    plan = result.get("recovery_plan")
    if plan is not None:
        cycle = getattr(plan, "cycle", None)
        if cycle is not None:
            return cycle
    snapshot = result.get("snapshot")
    daily = result.get("daily")
    if snapshot is not None and daily is not None:
        try:
            return monitor.analyze_cycle(daily, snapshot.price)
        except (ValueError, KeyError, TypeError):
            return None
    return None


def render_monitor_panel(refresh_seconds: int) -> None:
    st.title("XRP/JPY · 回本波段计划")

    portfolio = get_portfolio()

    try:
        cooldown = monitor.AlertCooldown(monitor.ALERT_COOLDOWN_SECONDS)
        result = monitor.run_monitor_cycle(cooldown, portfolio)
    except Exception as exc:
        st.error(f"数据获取失败：{exc}")
        return

    snapshot: monitor.MarketSnapshot = result["snapshot"]
    plan: monitor.RecoveryPlan = result["recovery_plan"]
    cycle = _resolve_cycle(result)
    technical = result.get("technical") or getattr(plan, "technical", None)
    current_action: monitor.CurrentAction = result["current_action"]
    advice: list[monitor.TradeAdvice] = result["advice"]
    signals: list[monitor.Signal] = result["signals"]
    push_results = result["push_results"]
    chart_data = result.get("chart_data")
    updated_at: datetime = result["updated_at"]

    st.caption(
        f"数据源: {snapshot.data_source} · RSI {snapshot.daily_rsi:.1f} · "
        f"自动刷新 {refresh_seconds}s · 静默 {monitor.quiet_hours_label()}"
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
    top[1].metric("30日区间", f"{monitor.fmt_jpy(snapshot.low_30d)} – {monitor.fmt_jpy(snapshot.high_30d)}")

    render_current_action(current_action, updated_at, snapshot)
    if technical is not None:
        render_technical_context(technical, snapshot)
    if cycle is not None:
        render_cycle_context(cycle, snapshot)
    render_portfolio(portfolio, snapshot, plan)
    render_recovery_plan(plan)
    if cycle is not None:
        render_swing_plan(plan, snapshot.price)
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
    st.set_page_config(page_title="XRP/JPY 回本监控", page_icon="📊", layout="wide")
    st.markdown(ACTION_CSS, unsafe_allow_html=True)
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
            help="原始投入总额",
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
