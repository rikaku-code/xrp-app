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
.panel-banner {
    border-left: 5px solid;
    border-radius: 10px;
    padding: 0.85rem 1rem;
    margin: -0.25rem 0 1rem;
}
.panel-banner-title {
    font-size: 1.28rem;
    font-weight: 800;
    line-height: 1.25;
    letter-spacing: 0.02em;
}
.panel-banner-sub {
    font-size: 0.88rem;
    opacity: 0.82;
    margin-top: 0.35rem;
    line-height: 1.45;
}
div[data-testid="stVerticalBlockBorderWrapper"] {
    border-radius: 14px !important;
    padding: 1rem 1.15rem 1.1rem !important;
    margin-bottom: 1.35rem !important;
    background: rgba(255, 255, 255, 0.02) !important;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.22) !important;
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
    "book_macd_divergence": ("success", "📘 MACD 底背离"),
    "book_selling_climax": ("success", "📘 Selling Climax"),
    "book_trend_follow_buy": ("success", "📘 ADX 追买"),
    "book_scale_in": ("success", "📘 顺势加仓"),
    "book_partial_take_profit": ("warning", "📘 阶段性止盈"),
    "book_target_profit": ("warning", "📘 目标位止盈"),
    "book_trailing_stop": ("warning", "📘 移动止损"),
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
            "HOLDINGS_BTC",
            "AVAILABLE_JPY",
            "TARGET_JPY",
        ):
            if key in st.secrets:
                os.environ[key] = str(st.secrets[key])
        monitor.reload_config()
    except (FileNotFoundError, AttributeError, RuntimeError):
        pass


def init_session_state() -> None:
    if "alert_log" not in st.session_state:
        st.session_state.alert_log = []
    if "toast_keys" not in st.session_state:
        st.session_state.toast_keys = set()
    # 新会话（含浏览器刷新）从磁盘恢复；同会话内不覆盖
    if "_portfolio_initialized" not in st.session_state:
        pf = monitor.load_persisted_portfolio()
        st.session_state.holdings_xrp = float(pf.xrp_quantity)
        st.session_state.holdings_btc = float(pf.btc_quantity)
        st.session_state.available_jpy = float(pf.cash_jpy)
        st.session_state.target_jpy = float(pf.target_jpy)
        st.session_state._portfolio_initialized = True


def sync_portfolio_state(portfolio: monitor.Portfolio) -> None:
    st.session_state.holdings_xrp = float(portfolio.xrp_quantity)
    st.session_state.holdings_btc = float(portfolio.btc_quantity)
    st.session_state.available_jpy = float(portfolio.cash_jpy)
    st.session_state.target_jpy = float(portfolio.target_jpy)
    monitor.save_persisted_portfolio(portfolio)


def reset_portfolio_from_config() -> None:
    monitor.clear_persisted_portfolio()
    sync_portfolio_state(monitor.load_portfolio())


def get_portfolio() -> monitor.Portfolio:
    return monitor.Portfolio(
        xrp_quantity=st.session_state.holdings_xrp,
        cash_jpy=st.session_state.available_jpy,
        target_jpy=st.session_state.target_jpy,
        btc_quantity=float(st.session_state.get("holdings_btc", 0.0)),
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
    if unit == "JPY":
        return monitor.fmt_jpy(value)
    if unit == "BTC":
        return f"{value:,.4f} {unit}"
    if unit == "XRP":
        return f"{value:,.1f} {unit}"
    return f"{value:,.4f} {unit}"


def render_current_action(
    action: monitor.CurrentAction,
    updated_at: datetime,
    snapshot: monitor.MarketSnapshot,
) -> None:
    css_class, badge_text = ACTION_BADGE.get(action.action, ("wait", "⏸ 等待"))
    buy_jpy = _fmt_qty(action.buy_jpy, "JPY")
    coin = action.asset or "XRP"
    buy_coin = _fmt_qty(action.buy_xrp, coin)
    sell_coin = _fmt_qty(action.sell_xrp, coin)
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
      <div class="action-metric-label">买入数量 ({coin})</div>
      <div class="action-metric-value{buy_xrp_cls}">{buy_coin}</div>
    </div>
    <div class="action-metric">
      <div class="action-metric-label">卖出数量 ({coin})</div>
      <div class="action-metric-value{sell_xrp_cls}">{sell_coin}</div>
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


def render_sidebar_portfolio() -> None:
    """左侧栏编辑持仓，点「保存」后生效（自动刷新不会冲掉未保存的草稿）。"""
    st.header("我的资产")
    pf = get_portfolio()
    with st.form("portfolio_form"):
        xrp = st.number_input(
            "XRP 持仓（枚）",
            min_value=0.0,
            value=float(pf.xrp_quantity),
            step=1.0,
            format="%.0f",
        )
        btc = st.number_input(
            "BTC 持仓（枚）",
            min_value=0.0,
            value=float(pf.btc_quantity),
            step=0.0001,
            format="%.4f",
            help="用于 BTC 书本止盈/止损与操作卡片",
        )
        cash = st.number_input(
            "日元可支配资产",
            min_value=0.0,
            value=float(pf.cash_jpy),
            step=1000.0,
            format="%.0f",
            help="可用于买入 XRP 的现金",
        )
        target = st.number_input(
            "回本目标（日元）",
            min_value=0.0,
            value=float(pf.target_jpy),
            step=10000.0,
            format="%.0f",
            help="原始投入总额",
        )
        if st.form_submit_button("保存持仓", use_container_width=True):
            sync_portfolio_state(
                monitor.Portfolio(
                    xrp_quantity=xrp,
                    btc_quantity=btc,
                    cash_jpy=cash,
                    target_jpy=target,
                )
            )
            append_alert_log(
                f"持仓已保存：{xrp:,.0f} XRP · {btc:.4f} BTC · 现金 {monitor.fmt_jpy(cash)} · "
                f"目标 {monitor.fmt_jpy(target)}",
                "success",
            )
            st.rerun()

    saved = get_portfolio()
    saved_at = monitor.portfolio_saved_at()
    saved_hint = f" · 保存于 {saved_at}" if saved_at else ""
    st.caption(
        f"当前生效：{saved.xrp_quantity:,.0f} XRP · {saved.btc_quantity:.4f} BTC · "
        f"{monitor.fmt_jpy(saved.cash_jpy)} · "
        f"单次低吸建议 {monitor.fmt_jpy(monitor.suggest_dca_jpy(saved.cash_jpy))}{saved_hint}"
    )
    if st.button("重置为 Secrets / .env 默认值", use_container_width=True):
        reset_portfolio_from_config()
        append_alert_log("已恢复为配置文件默认持仓", "info")
        st.rerun()


def render_market_technicals(
    title: str,
    tech: monitor.TechnicalContext,
    snapshot: monitor.MarketSnapshot,
    book: monitor.BookStrategyContext | None = None,
    *,
    book_label: str = "书本策略",
    book_hint: str = "",
) -> None:
    st.markdown(f"#### {title}")
    cols = st.columns(6)
    cols[0].metric("RSI(14)", f"{tech.rsi:.0f}", tech.rsi_zone)
    stoch_label = "金叉" if tech.stoch_golden else ("死叉" if tech.stoch_dead else "—")
    cols[1].metric("Stoch %K", f"{tech.stoch_k:.0f}", f"D={tech.stoch_d:.0f} · {stoch_label}")
    macd_label = "上穿零轴" if tech.macd_hist_bullish else ("下穿零轴" if tech.macd_hist_bearish else "—")
    cols[2].metric("MACD 柱", f"{tech.macd_hist:+.2f}", macd_label)
    if book:
        cols[3].metric("ADX(14)", f"{book.adx:.0f}", "强趋势" if book.adx_strong else "弱趋势")
        cols[4].metric("量比", f"{book.volume_ratio:.1f}×", "均量 20 日")
        cols[5].metric("实体 MA5", monitor.fmt_jpy(book.body_ma))
    else:
        cols[3].metric("MA20", monitor.fmt_jpy(snapshot.ma20))
        cols[4].metric("MA50", monitor.fmt_jpy(snapshot.ma50))
        cols[5].metric("布林带", f"{monitor.fmt_jpy(snapshot.bb_lower)}–{monitor.fmt_jpy(snapshot.bb_upper)}")

    sig_cols = st.columns(2)
    if tech.buy_triggered:
        sig_cols[0].success(f"偏多：{tech.buy_reason}")
    else:
        sig_cols[0].info(f"偏多：未满足 — {tech.buy_reason}")
    if tech.sell_triggered:
        sig_cols[1].warning(f"偏空：{tech.sell_reason}")
    else:
        sig_cols[1].info(f"偏空：未满足 — {tech.sell_reason}")

    st.caption(
        f"MA20 {monitor.fmt_jpy(snapshot.ma20)} · MA50 {monitor.fmt_jpy(snapshot.ma50)} · "
        f"布林带 {monitor.fmt_jpy(snapshot.bb_lower)} – {monitor.fmt_jpy(snapshot.bb_upper)} · "
        f"数据源 {snapshot.data_source}"
    )

    if book and (book.buy_alerts or book.sell_alerts):
        st.markdown(f"**{book_label}**")
        if book_hint:
            st.caption(book_hint)
        for msg in book.buy_alerts:
            st.success(msg)
        for msg in book.sell_alerts:
            st.warning(msg)
    elif book:
        st.caption(f"{book_label}：暂无触发项")


def render_technical_context(
    tech: monitor.TechnicalContext,
    snapshot: monitor.MarketSnapshot,
    book: monitor.BookStrategyContext | None = None,
) -> None:
    render_market_technicals(
        "技术指标 · XRP 操作依据",
        tech,
        snapshot,
        book,
        book_label="XRP 书本策略（触发 Lark / 操作卡片）",
        book_hint="以下信号会纳入 XRP 买卖判断与推送。",
    )


def _panel_banner(title: str, subtitle: str, accent: str) -> None:
    st.markdown(
        f"""
<div class="panel-banner" style="
    border-left-color:{accent};
    background:linear-gradient(90deg,{accent}30,transparent);
">
  <div class="panel-banner-title">{title}</div>
  <div class="panel-banner-sub">{subtitle}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def render_trend_summary(
    ctx: monitor.AssetTrendContext,
    *,
    price_label: str = "价格",
    strength_label: str = "趋势强度",
    operation_note: str | None = None,
    show_cycle: bool = True,
) -> None:
    """BTC / XRP 共用的趋势判定面板。"""
    trend_icon = {"上涨": "📈", "下跌": "📉", "震荡": "↔️"}.get(ctx.trend, "—")
    cols = st.columns(3)
    cols[0].metric(price_label, monitor.fmt_jpy(ctx.snapshot.price))
    cols[1].metric("趋势判定", f"{trend_icon} {ctx.trend}", f"{strength_label} {ctx.xrp_bias:+.2f}")
    cols[2].metric(
        "30日区间",
        f"{monitor.fmt_jpy(ctx.snapshot.low_30d)} – {monitor.fmt_jpy(ctx.snapshot.high_30d)}",
    )
    if show_cycle:
        c = ctx.cycle
        st.caption(
            f"4年周期（参考）：**{c.phase}** · 位置 {c.position_pct:.0f}% · "
            f"{monitor.fmt_jpy(c.range_low)}–{monitor.fmt_jpy(c.range_high)}"
        )

    note = operation_note or ""
    if ctx.trend == "上涨":
        st.success(note or f"趋势{ctx.trend} · 偏多")
    elif ctx.trend == "下跌":
        st.warning(note or f"趋势{ctx.trend} · 偏空")
    else:
        st.info(note or f"趋势{ctx.trend} · 观望波段")
    st.caption(f"趋势依据：{ctx.trend_detail}")


def render_coin_portfolio(
    coin: str,
    portfolio: monitor.Portfolio,
    snapshot: monitor.MarketSnapshot,
    plan: monitor.RecoveryPlan,
) -> None:
    st.markdown("##### 资产概况")
    if coin == "BTC":
        qty = portfolio.btc_quantity
        value = portfolio.btc_value(snapshot.price)
        qty_label = f"{qty:,.4f} BTC"
    else:
        qty = portfolio.xrp_quantity
        value = portfolio.xrp_value(snapshot.price)
        qty_label = f"{qty:,.0f} XRP"
    cols = st.columns(4)
    cols[0].metric(f"{coin} 持仓", qty_label, monitor.fmt_jpy(value))
    cols[1].metric("日元现金（共用）", monitor.fmt_jpy(portfolio.cash_jpy))
    cols[2].metric(f"{coin} 市值", monitor.fmt_jpy(value))
    cols[3].metric("单次低吸建议", monitor.fmt_jpy(plan.dca_buy_jpy))
    if portfolio.target_jpy > 0 and coin == "XRP":
        pnl = portfolio.pnl(snapshot.price)
        st.caption(
            f"相对投入目标 {monitor.fmt_jpy(portfolio.target_jpy)}："
            f"盈亏 {monitor.fmt_jpy(pnl)}（{portfolio.pnl_pct(snapshot.price):+.1f}%）"
        )


def render_btc_section(
    btc_trend: monitor.AssetTrendContext | None,
    btc_snapshot: monitor.MarketSnapshot | None,
    btc_current_action: monitor.CurrentAction | None,
    updated_at: datetime,
    btc_technical: monitor.TechnicalContext | None,
    btc_book: monitor.BookStrategyContext | None,
    portfolio: monitor.Portfolio,
    btc_plan: monitor.RecoveryPlan | None,
    btc_advice: list[monitor.TradeAdvice] | None,
    btc_chart_data,
    btc_signals: list[monitor.Signal] | None,
    cooldown: monitor.AlertCooldown,
) -> None:
    with st.container(border=True):
        _panel_banner(
            "🟠 ① BTC/JPY 操作",
            "与 XRP 相同：RSI / 书本策略 / 操作卡片 / Lark（不含 4 年周期表）",
            "#f7931a",
        )

        if btc_trend is None or btc_snapshot is None:
            st.warning("BTC 数据暂不可用。")
            return

        render_trend_summary(
            btc_trend,
            price_label="BTC 价格",
            strength_label="强度",
            operation_note=monitor.btc_trend_operation_note(btc_trend),
            show_cycle=False,
        )

        if btc_current_action is not None:
            render_current_action(btc_current_action, updated_at, btc_snapshot)
            render_action_trend_note(
                btc_trend, btc_current_action, btc_book, asset_label="BTC"
            )

        if btc_technical is not None:
            render_market_technicals(
                "BTC 技术指标 · 操作依据",
                btc_technical,
                btc_snapshot,
                btc_book,
                book_label="BTC 书本策略（触发 Lark / 操作卡片）",
                book_hint="与 XRP 相同逻辑；请在侧边栏填写 BTC 持仓以启用止盈/止损。",
            )

        if btc_plan is not None:
            render_coin_portfolio("BTC", portfolio, btc_snapshot, btc_plan)
            render_swing_plan(btc_plan, btc_snapshot.price, coin="BTC", cycle_table=False)
        if btc_advice:
            render_trade_advice(btc_advice)
        if btc_chart_data is not None:
            render_price_chart(btc_chart_data, title="BTC 价格走势", show_cycle_lines=False)
        if btc_signals:
            render_signals(btc_signals, cooldown, section_title="BTC Lark 推送")


def render_action_trend_note(
    xrp_trend: monitor.AssetTrendContext | None,
    current_action: monitor.CurrentAction,
    book: monitor.BookStrategyContext | None,
    *,
    asset_label: str = "XRP",
) -> None:
    """趋势判定与操作卡片不一致时说明原因。"""
    if xrp_trend is None:
        return
    trend = xrp_trend.trend
    act = current_action.action
    if trend == "上涨" and act == "卖出":
        st.info(
            "📌 **趋势 vs 操作**：中长期偏多（价≥MA20、MACD>0），"
            f"但当前触发 **{current_action.title}**（短期/风控优先）。"
            " 趋势看方向，黄卡看此刻要不要动仓，两者可以不一致。"
            f"\n\n触发原因：{current_action.reason}"
        )
    elif trend == "下跌" and act == "买入":
        st.warning(
            f"📌 **趋势 vs 操作**：{asset_label} 趋势偏空，但仍出现买入信号。"
            f" 请结合书本条件谨慎执行。\n\n{current_action.reason}"
        )
    elif act == "等待" and trend in ("上涨", "下跌"):
        st.caption(
            f"趋势判定为「{trend}」，但 RSI/Stoch/书本尚未同时满足操作阈值 → 暂时等待。"
        )
    if book and book.trailing_stop_exit and trend == "上涨":
        st.caption(
            "移动止损：收盘价低于实体 MA5（约 5 日短线），属于书本图 123/125 的短线离场规则，"
            "不等于中长期趋势转空。"
        )


def render_xrp_section(
    snapshot: monitor.MarketSnapshot,
    xrp_trend: monitor.AssetTrendContext | None,
    btc_trend: monitor.AssetTrendContext | None,
    current_action: monitor.CurrentAction,
    updated_at: datetime,
    technical: monitor.TechnicalContext | None,
    book: monitor.BookStrategyContext | None,
    cycle: monitor.CycleContext | None,
    portfolio: monitor.Portfolio,
    plan: monitor.RecoveryPlan,
    advice: list[monitor.TradeAdvice],
    chart_data,
    signals: list[monitor.Signal],
    cooldown: monitor.AlertCooldown,
) -> None:
    with st.container(border=True):
        _panel_banner(
            "🔵 ② XRP/JPY 持仓操作",
            "你的实际持仓与买卖信号 · 操作卡片、Lark 推送均以此为准",
            "#5eb3ff",
        )

        if xrp_trend is not None:
            render_trend_summary(
                xrp_trend,
                price_label="XRP 价格",
                strength_label="强度",
                operation_note=monitor.xrp_trend_operation_note(xrp_trend, btc_trend),
            )
        else:
            cols = st.columns(2)
            cols[0].metric("XRP 价格", monitor.fmt_jpy(snapshot.price))
            cols[1].metric(
                "30日区间",
                f"{monitor.fmt_jpy(snapshot.low_30d)} – {monitor.fmt_jpy(snapshot.high_30d)}",
            )

        render_current_action(current_action, updated_at, snapshot)
        render_action_trend_note(xrp_trend, current_action, book)

        if technical is not None:
            render_technical_context(technical, snapshot, book)
        if book is None and getattr(monitor, "analyze_book_strategies", None) is None:
            st.caption("⚠️ XRP 书本策略未加载：请同步最新 `xrp_monitor.py`。")
        render_coin_portfolio("XRP", portfolio, snapshot, plan)
        if cycle is not None:
            render_cycle_context(cycle)
            render_swing_plan(plan, snapshot.price, coin="XRP", cycle_table=True)
        render_trade_advice(advice)
        render_price_chart(chart_data)
        render_signals(signals, cooldown)


def render_cycle_context(cycle: monitor.CycleContext) -> None:
    label = (
        f"4年周期详情 · {cycle.phase}（{cycle.position_pct:.0f}%）· "
        f"回落 {cycle.drawdown_pct:.0f}% · {cycle.halving_label}"
    )
    with st.expander(label, expanded=False):
        st.progress(cycle.position_pct / 100)
        st.caption(
            f"{cycle.phase_detail} · {cycle.month}月{cycle.month_strength}（月均 {cycle.month_return_pct:+.1f}%）"
            f" · {cycle.month_history} · 仅供挂单参考，须 RSI 确认"
        )


def render_swing_plan(
    plan: monitor.RecoveryPlan,
    current_price: float,
    *,
    coin: str = "XRP",
    cycle_table: bool = True,
) -> None:
    title = "周期参考价位（需 RSI 确认）" if cycle_table else "30 日波段参考价位（需 RSI 确认）"
    st.markdown(f"##### {title}")
    st.caption("挂单参考位，**不等于立即买卖**。")
    col_buy, col_sell = st.columns(2)

    with col_buy:
        st.markdown("**参考低吸位**")
        if plan.buy_steps:
            for step in plan.buy_steps:
                near = abs(current_price - step.trigger_price) / step.trigger_price < 0.03
                marker = "📍 " if near else ""
                qty_line = (
                    f"{step.amount_xrp:,.4f} {coin}"
                    if coin == "BTC"
                    else f"{step.amount_xrp:,.1f} {coin}"
                )
                st.markdown(
                    f"""
<div class="plan-step-card buy-step">
  <div class="plan-step-price">{marker}{monitor.fmt_jpy(step.trigger_price)} · {step.trigger_label}</div>
  <div class="plan-step-qty">{step.amount_desc} · {qty_line}</div>
  <div style="font-size:0.85rem;opacity:0.85;margin-top:0.3rem;">{step.result_desc}</div>
</div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无参考低吸位")

    with col_sell:
        st.markdown("**参考高抛位**")
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
        if item.action == "持有" and item.strength in ("参考", "技术", "周期", "BTC", "书本"):
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


def render_price_chart(
    chart_data,
    *,
    title: str = "XRP 价格走势",
    show_cycle_lines: bool = True,
) -> None:
    if chart_data is None or chart_data.empty:
        return
    st.markdown(f"#### {title}")
    st.line_chart(chart_data, height=240)
    if show_cycle_lines and chart_data is not None and "p25" in chart_data.columns:
        st.caption("参考线：4年 25% / 75% 分位（周期低吸/高抛带）")


def _signal_style(key: str) -> tuple[str, str]:
    if key in SIGNAL_STYLE:
        return SIGNAL_STYLE[key]
    base = key.removeprefix("btc_")
    return SIGNAL_STYLE.get(base, ("info", key))


def render_signals(
    signals: list[monitor.Signal],
    cooldown: monitor.AlertCooldown,
    *,
    section_title: str = "XRP Lark 推送",
) -> None:
    if not signals:
        return
    st.markdown(f"#### {section_title}")
    for signal in signals:
        style, _ = _signal_style(signal.key)
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


def _resolve_book(result: dict) -> monitor.BookStrategyContext | None:
    """兼容 Cloud 上旧版 xrp_monitor（RecoveryPlan 无 book 字段）。"""
    book = result.get("book")
    if book is not None:
        return book
    plan = result.get("recovery_plan")
    if plan is not None:
        book = getattr(plan, "book", None)
        if book is not None:
            return book
    analyze = getattr(monitor, "analyze_book_strategies", None)
    if analyze is None:
        return None
    snapshot = result.get("snapshot")
    daily = result.get("daily")
    portfolio = result.get("portfolio")
    cycle = _resolve_cycle(result)
    if snapshot is None or daily is None or portfolio is None or cycle is None:
        return None
    try:
        return analyze(daily, snapshot, portfolio, cycle)
    except (ValueError, KeyError, TypeError, AttributeError):
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
    book = _resolve_book(result)
    technical = result.get("technical") or getattr(plan, "technical", None)
    current_action: monitor.CurrentAction = result["current_action"]
    advice: list[monitor.TradeAdvice] = result["advice"]
    signals: list[monitor.Signal] = result["signals"]
    push_results = result["push_results"]
    chart_data = result.get("chart_data")
    updated_at: datetime = result["updated_at"]
    btc = result.get("btc_trend")
    xrp_trend = result.get("xrp_trend")
    if xrp_trend is None and technical is not None and book is not None and cycle is not None:
        try:
            xrp_trend = monitor.build_trend_context(
                monitor.XRP_MARKET.label, snapshot, cycle, technical, book
            )
        except (AttributeError, TypeError, ValueError):
            xrp_trend = None

    st.caption(
        f"自动刷新 {refresh_seconds}s · 静默 {monitor.quiet_hours_label()} · "
        f"XRP 数据源 {snapshot.data_source}"
    )
    if monitor.is_quiet_hours():
        st.info(f"🌙 静默时段（{monitor.quiet_hours_label()}），Lark 暂不推送。")

    handle_push_results(push_results)

    for signal in signals:
        toast_key = f"ui_{signal.key}"
        if toast_key not in st.session_state.toast_keys:
            icon = "🟠" if signal.key.startswith("btc_") or getattr(signal, "market", "") == "BTC" else "🔔"
            st.toast(signal.console_msg, icon=icon)
            st.session_state.toast_keys.add(toast_key)

    btc_signals = [
        s for s in signals if getattr(s, "market", "XRP") == "BTC" or s.key.startswith("btc_")
    ]
    xrp_signals = [s for s in signals if s not in btc_signals]

    # ── ① BTC（上）────────────────────────────────────────────
    render_btc_section(
        btc,
        result.get("btc_snapshot"),
        result.get("btc_current_action"),
        updated_at,
        result.get("btc_technical"),
        result.get("btc_book") or (btc.book if btc else None),
        portfolio,
        result.get("btc_recovery_plan"),
        result.get("btc_advice"),
        result.get("btc_chart_data"),
        btc_signals,
        cooldown,
    )

    # ── ② XRP（下）────────────────────────────────────────────
    render_xrp_section(
        snapshot,
        xrp_trend,
        btc,
        current_action,
        updated_at,
        technical,
        book,
        cycle,
        portfolio,
        plan,
        advice,
        chart_data,
        xrp_signals,
        cooldown,
    )

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
        render_sidebar_portfolio()

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
