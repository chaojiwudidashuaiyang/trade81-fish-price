from __future__ import annotations

import os
import pandas as pd
import streamlit as st

from price_parser import group_price_rows, read_price_template
from price_store import (
    apply_sheet, configure_database, delete_product_group, export_backup_xlsx,
    import_backup_xlsx, list_history, list_prices, list_product_groups,
    manual_update, record_order_counts, resolve_sheet_products, save_product_group,
)


MARKUP = 0.05
st.set_page_config(page_title="Trade81 海鲜价格库", page_icon="🐟", layout="wide")


def secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def secret_section(name):
    value = secret(name, {})
    return value if value else {}


def table_height(row_count: int, cap: int = 1400) -> int:
    return min(cap, max(180, 42 + row_count * 34))


database_url = secret("SUPABASE_DB_URL") or os.getenv("SUPABASE_DB_URL")
configure_database(database_url)
local_mode = bool(os.getenv("LOCALAPPDATA") or os.getenv("TRADE81_LOCAL_MODE") == "1")
auth_config = secret_section("auth")
access_config = secret_section("access")

st.title("🐟 Trade81 海鲜价格库")
st.caption("价格按日元显示 · 实际进货成本 = 鱼商报价 × 1.05")

if not database_url and not local_mode:
    st.error("共享数据库尚未配置。请在部署平台的 Secrets 中设置 SUPABASE_DB_URL。")
    st.stop()

if auth_config:
    try:
        logged_in = bool(st.user.is_logged_in)
    except Exception:
        logged_in = False
    if not logged_in:
        st.info("请使用获准的 Google 账号登录。")
        if st.button("使用 Google 登录", type="primary"):
            st.login()
        st.stop()
    email = str(st.user.get("email", "")).strip().lower()
    admins = {str(value).strip().lower() for value in access_config.get("admin_emails", [])}
    viewers = {str(value).strip().lower() for value in access_config.get("viewer_emails", [])}
    if email in admins:
        role = "admin"
    elif email in viewers:
        role = "viewer"
    else:
        st.error(f"账号 {email or '(未返回邮箱)'} 尚未加入价格库访问名单。")
        if st.button("退出登录"):
            st.logout()
        st.stop()
else:
    if not local_mode:
        st.error("登录配置尚未完成。请在部署 Secrets 中配置 Google OAuth 与访问名单。")
        st.stop()
    email, role = "本机管理员", "admin"

with st.sidebar:
    st.header("价格库管理")
    st.caption("管理员工具 · 只上传当天最新价格表")
    if auth_config:
        st.caption("当前账号：" + ("管理员" if role == "admin" else "只读查看"))
        if st.button("退出登录", key="sidebar_logout"):
            st.logout()

    if role == "admin":
        current = list_prices()
        with st.expander("更新当天价格", expanded=True):
            st.caption("上传当天价格表即可。文件中没有的商品沿用库内价格；同日重复上传会替换该日的下单次数。")
            price_file = st.file_uploader("今日最新价格表（.xlsx）", type=["xlsx"], key="today_price_file")
            if st.button("更新价格库", type="primary", disabled=price_file is None, use_container_width=True):
                try:
                    price_file.seek(0)
                    incoming = read_price_template(price_file)
                    if not incoming.get("date"):
                        raise ValueError("无法从价格表第 5 行读取日期。")
                    resolved = resolve_sheet_products(incoming)
                    unique, conflicts = group_price_rows(resolved)
                    allowed_rows = [row for row in resolved["rows"] if row["key"] not in conflicts]
                    price_stats = apply_sheet({**resolved, "rows": allowed_rows}, updated_by=email)
                    order_lines = record_order_counts(resolved)
                    conflict_names = [
                        next((row["canonical_name"] for row in resolved["rows"] if row["key"] == key), key)
                        for key in conflicts
                    ]
                    message = (
                        f"{incoming['date']} 已处理：价格新增或更新 {price_stats['changed']} 项，"
                        f"未变或较旧而跳过 {price_stats['skipped']} 项；累计下单统计已记录 {order_lines} 行。"
                    )
                    if conflict_names:
                        message += " 报价冲突、未自动更新的归并商品：" + "、".join(conflict_names)
                    st.session_state["price_flash"] = message
                    st.rerun()
                except Exception as exc:
                    st.error(f"读取或更新失败：{exc}")

        with st.expander("归并相同品种", expanded=False):
            st.caption("把不同写法归到同一商品名。每行填写一个别名；归并后价格和下单次数合并显示。")
            with st.form("product_group_form", clear_on_submit=True):
                primary = st.text_input("主要显示品名", placeholder="例如：みかん鯛")
                aliases_text = st.text_area(
                    "要归并的其他品名（每行一个）",
                    placeholder="蜜柑鲷\n養殖ミカンタイ みかん鯛 1.8 kg 1",
                    height=100,
                )
                save_group = st.form_submit_button("保存归并规则", use_container_width=True)
            if save_group:
                try:
                    names = [line.strip() for line in aliases_text.splitlines() if line.strip()]
                    save_product_group(primary, names)
                    st.session_state["price_flash"] = f"已将相关写法归并到「{primary.strip()}」。"
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存归并失败：{exc}")
            groups = list_product_groups()
            if groups:
                st.markdown("**已设置的归并**")
                for index, group in enumerate(groups):
                    aliases_label = "、".join(group["aliases"]) or "暂无别名"
                    st.caption(f"{group['canonical_name']} ← {aliases_label}")
                    if st.button("取消此归并", key=f"ungroup_{index}"):
                        delete_product_group(group["canonical_key"])
                        st.session_state["price_flash"] = f"已取消「{group['canonical_name']}」的归并；原始商品记录已恢复显示。"
                        st.rerun()

        if current:
            with st.expander("手动修改价格", expanded=False):
                by_key = {row["product_key"]: row for row in current}
                selected = st.selectbox(
                    "选择商品", list(by_key),
                    format_func=lambda key: by_key[key]["product_name"],
                    key="manual_product",
                )
                selected_row = by_key[selected]
                with st.form("manual_price_edit"):
                    edited_quote = st.number_input(
                        "鱼商报价（¥）", min_value=0.0,
                        value=float(selected_row["supplier_quote_jpy"]), step=100.0,
                    )
                    st.caption("归并商品会同时修改组内各原始商品；后续较新日期的价格表仍可更新它们。")
                    if st.form_submit_button("保存手动价格", use_container_width=True):
                        manual_update(selected, edited_quote, updated_by=email, member_keys=selected_row["member_keys"])
                        st.session_state["price_flash"] = "已保存手动价格。"
                        st.rerun()

        st.download_button(
            "下载价格库备份",
            data=export_backup_xlsx(),
            file_name="Trade81_价格库备份.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
        if database_url and not current:
            with st.expander("从本机迁移旧价格库", expanded=False):
                st.caption("仅用于首次迁移到空云端价格库。")
                backup_file = st.file_uploader("备份文件（.xlsx）", type=["xlsx"], key="migration_backup")
                if st.button("导入旧价格与历史", disabled=backup_file is None):
                    try:
                        count = import_backup_xlsx(backup_file)
                        st.session_state["price_flash"] = f"已迁移 {count} 条商品价格及更新记录。"
                        st.rerun()
                    except Exception as exc:
                        st.error(f"迁移失败：{exc}")

try:
    current = list_prices()
except Exception as exc:
    st.error(f"无法连接价格数据库：{exc}")
    st.stop()

if st.session_state.get("price_flash"):
    st.success(st.session_state.pop("price_flash"))

st.subheader("海鲜价格与下单热度")
if current:
    total_order_occurrences = sum(int(row.get("order_count", 0) or 0) for row in current)
    latest_catalog_date = max((str(row.get("source_date", "")) for row in current), default="—")
    metric_cols = st.columns(3)
    metric_cols[0].metric("商品品种数", f"{len(current):,}")
    metric_cols[1].metric("累计下单行数", f"{total_order_occurrences:,}")
    metric_cols[2].metric("最近价格表日期", latest_catalog_date or "—")

    filter_col, sort_col = st.columns([3, 1])
    search = filter_col.text_input("搜索品种 / Size", key="catalog_search", placeholder="例如：ハマチ、350g")
    sort_mode = sort_col.selectbox(
        "排列方式", ["下单次数（高到低）", "最近更新时间（新到旧）", "品种名称（A 到 Z）"],
        key="catalog_sort",
    )
    shown = [row for row in current if not search or search.casefold() in row["product_name"].casefold() or any(search.casefold() in name.casefold() for name in row.get("aliases", []))]
    if sort_mode == "最近更新时间（新到旧）":
        shown.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
    elif sort_mode == "品种名称（A 到 Z）":
        shown.sort(key=lambda row: row["product_name"].casefold())
    else:
        shown.sort(key=lambda row: (-int(row.get("order_count", 0) or 0), row["product_name"].casefold()))

    catalog_df = pd.DataFrame([{
        "品种 / Size": row["product_name"],
        "下单次数": int(row.get("order_count", 0) or 0),
        "鱼商报价(¥)": row["supplier_quote_jpy"],
        "加 5% 后(¥)": round(row["supplier_quote_jpy"] * (1 + MARKUP), 2),
        "价格表日期": row["source_date"],
        "最近更新时间": row["updated_at"],
    } for row in shown])
    st.dataframe(catalog_df, use_container_width=True, hide_index=True, height=table_height(len(catalog_df)), column_config={
        "品种 / Size": st.column_config.TextColumn(width="large"),
        "下单次数": st.column_config.NumberColumn(width="small", format="%d"),
        "鱼商报价(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
        "加 5% 后(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
        "价格表日期": st.column_config.TextColumn(width="small"),
        "最近更新时间": st.column_config.TextColumn(width="medium"),
    })
    st.caption("下单次数按价格表中的商品行累计；同一商品出现在不同餐厅的订单分别计次，不按数量折算。上传文件缺少的品种沿用价格库现有数据。")
else:
    st.info("价格库目前为空。管理员可在左侧迁移本机价格库，或上传当天价格表。")

st.subheader("最近价格更新记录")
history = list_history(100)
if history:
    history_search = st.text_input("筛选更新记录", key="history_search", placeholder="输入品种名称筛选")
    visible_history = [row for row in history if not history_search or history_search.casefold() in row["product_name"].casefold()]
    history_df = pd.DataFrame([{
        "品种 / Size": row["product_name"],
        "鱼商报价(¥)": row["supplier_quote_jpy"],
        "价格表日期": row["source_date"],
        "更新时间": row["updated_at"],
        "更新来源": row["source"],
        "操作人": row.get("updated_by", ""),
    } for row in visible_history])
    st.dataframe(history_df, use_container_width=True, hide_index=True, height=table_height(len(history_df)), column_config={
        "品种 / Size": st.column_config.TextColumn(width="large"),
        "鱼商报价(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
        "价格表日期": st.column_config.TextColumn(width="small"),
        "更新时间": st.column_config.TextColumn(width="medium"),
        "更新来源": st.column_config.TextColumn(width="small"),
        "操作人": st.column_config.TextColumn(width="medium"),
    })
else:
    st.caption("还没有价格更新记录。")
