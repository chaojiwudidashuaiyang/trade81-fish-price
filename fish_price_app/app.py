from __future__ import annotations

import os
from datetime import datetime
import pandas as pd
import streamlit as st

from price_parser import group_price_rows, read_price_template
from price_store import (
    apply_sheet, configure_database, delete_product_group, exclude_products, export_backup_xlsx,
    import_backup_xlsx, list_excluded_products, list_history, list_prices, list_product_groups,
    list_product_history, manual_update, merge_product_groups, record_order_counts,
    remove_product_from_group, resolve_sheet_products, restore_excluded_products,
)


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


def display_date(value) -> str:
    if not value:
        return ""
    try:
        day = datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            day = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return str(value)
    return f"{day.year % 100:02d}年{day.month}月{day.day}日"


database_url = secret("SUPABASE_DB_URL") or os.getenv("SUPABASE_DB_URL")
configure_database(database_url)
local_mode = bool(os.getenv("LOCALAPPDATA") or os.getenv("TRADE81_LOCAL_MODE") == "1")
auth_config = secret_section("auth")
access_config = secret_section("access")

st.title("🐟 Trade81 海鲜价格库")

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

try:
    current = list_prices()
except Exception as exc:
    st.error(f"无法连接价格数据库：{exc}")
    st.stop()

with st.sidebar:
    if role == "admin":
        st.header("价格库管理")
        st.caption("管理员工具 · 只上传当天最新价格表")
    if auth_config:
        if role == "admin":
            st.caption("当前账号：管理员")
        if st.button("退出登录", key="sidebar_logout"):
            st.logout()

    if role == "admin":
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
                    _unique, conflicts = group_price_rows(resolved)
                    allowed_rows = [row for row in resolved["rows"] if row["key"] not in conflicts]
                    price_stats = apply_sheet({**resolved, "rows": allowed_rows}, updated_by=email)
                    record_order_counts(resolved)
                    st.session_state.pop("price_backup_data", None)
                    conflict_names = [
                        next((row["canonical_name"] for row in resolved["rows"] if row["key"] == key), key)
                        for key in conflicts
                    ]
                    message = (
                        f"{incoming['date']} 已处理：价格新增或更新 {price_stats['changed']} 项，"
                        f"未变或较旧而跳过 {price_stats['skipped']} 项。"
                    )
                    message = (
                        f"{incoming['date']} 已处理：价格变更 {price_stats['changed']} 项，"
                        f"同价刷新日期 {price_stats['unchanged']} 项，较旧或同日已处理 {price_stats['skipped']} 项。"
                        + (" 报价冲突、未自动更新：" + "、".join(conflict_names) if conflict_names else "")
                    )
                    st.session_state["price_flash"] = message
                    st.rerun()
                except Exception as exc:
                    st.error(f"读取或更新失败：{exc}")

        st.markdown("**已设置的归并**")
        st.caption("每项默认折叠；展开后可查看、添加或移除品种。")
        with st.expander("新建品种归并", expanded=False):
            product_choices = {
                row["product_key"]: f"{row['product_name']}  ·  ¥{row['supplier_quote_jpy']:,.0f}"
                for row in current
            }
            with st.form("product_group_form", clear_on_submit=True):
                selected_keys = st.multiselect(
                    "选择要合并的现有品种（至少两项）",
                    options=list(product_choices),
                    format_func=lambda key: product_choices[key],
                )
                primary = st.text_input("合并后的品名", placeholder="例如：みかん鯛")
                save_group = st.form_submit_button(
                    "合并所选品种", use_container_width=True, disabled=len(product_choices) < 2
                )
            if save_group:
                try:
                    merge_product_groups(primary, selected_keys)
                    st.session_state.pop("price_backup_data", None)
                    st.session_state["price_flash"] = f"已将所选品种合并显示为「{primary.strip()}」。"
                    st.rerun()
                except Exception as exc:
                    st.error(f"保存归并失败：{exc}")
        groups = list_product_groups()
        for index, group in enumerate(groups):
            with st.expander(f"{group['canonical_name']} · {len(group['members'])} 个品种", expanded=False):
                for member_index, member in enumerate(group["members"]):
                    name_col, remove_col = st.columns([5, 1])
                    name_col.write(member["name"])
                    if remove_col.button("移除", key=f"remove_member_{index}_{member_index}"):
                        try:
                            remove_product_from_group(group["canonical_key"], member["key"])
                            st.session_state.pop("price_backup_data", None)
                            st.session_state["price_flash"] = f"已从「{group['canonical_name']}」移除「{member['name']}」。"
                            st.rerun()
                        except Exception as exc:
                            st.error(f"移除失败：{exc}")

                add_choices = {
                    row["product_key"]: f"{row['product_name']}  ·  ¥{row['supplier_quote_jpy']:,.0f}"
                    for row in current if row["product_key"] != group["canonical_key"]
                }
                if add_choices:
                    add_keys = st.multiselect(
                        "继续添加现有品种", options=list(add_choices),
                        format_func=lambda key: add_choices[key], key=f"add_members_{group['canonical_key']}",
                    )
                    if st.button("添加到此归并", key=f"add_group_{index}", disabled=not add_keys):
                        try:
                            merge_product_groups(group["canonical_name"], [group["canonical_key"], *add_keys])
                            st.session_state.pop("price_backup_data", None)
                            st.session_state["price_flash"] = f"已将所选品种添加到「{group['canonical_name']}」。"
                            st.rerun()
                        except Exception as exc:
                            st.error(f"添加失败：{exc}")
                if st.button("取消整个归并", key=f"ungroup_{index}"):
                    delete_product_group(group["canonical_key"])
                    st.session_state.pop("price_backup_data", None)
                    st.session_state["price_flash"] = f"已取消「{group['canonical_name']}」的归并；原商品恢复分别显示。"
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
                        st.session_state.pop("price_backup_data", None)
                        st.session_state["price_flash"] = "已保存手动价格。"
                        st.rerun()

        with st.expander("价格库备份", expanded=False):
            if st.button("准备备份文件", use_container_width=True):
                with st.spinner("正在整理价格和历史记录…"):
                    st.session_state["price_backup_data"] = export_backup_xlsx()
            if st.session_state.get("price_backup_data"):
                st.download_button(
                    "下载价格库备份",
                    data=st.session_state["price_backup_data"],
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
                        st.session_state.pop("price_backup_data", None)
                        st.session_state["price_flash"] = f"已迁移 {count} 条商品价格及更新记录。"
                        st.rerun()
                    except Exception as exc:
                        st.error(f"迁移失败：{exc}")

if st.session_state.get("price_flash"):
    st.success(st.session_state.pop("price_flash"))

st.subheader("最近 7 天有价格变化的商品")
recent_history = list_history(100, days=7)
if recent_history:
    recent_df = pd.DataFrame([{
        "品种": row["product_name"],
        "鱼商报价(¥)": row["supplier_quote_jpy"],
        "更新日期": display_date(row["updated_at"]),
    } for row in recent_history])
    st.dataframe(recent_df, use_container_width=True, hide_index=True, height=table_height(len(recent_df)), column_config={
        "品种": st.column_config.TextColumn(width="medium"),
        "鱼商报价(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
        "更新日期": st.column_config.TextColumn(width="small"),
    })
else:
    st.caption("最近 7 天没有记录到价格变化。")

st.subheader("海鲜最新报价与下单次数")
if current:
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
        "品种": row["product_name"],
        "下单次数": int(row.get("order_count", 0) or 0),
        "鱼商报价(¥)": row["supplier_quote_jpy"],
        "更新日期": display_date(row["updated_at"]),
    } for row in shown])
    catalog_event = st.dataframe(catalog_df, use_container_width=True, hide_index=True, height=table_height(len(catalog_df)), column_config={
        "品种": st.column_config.TextColumn(width="medium"),
        "下单次数": st.column_config.NumberColumn(width="small", format="%d"),
        "鱼商报价(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
        "更新日期": st.column_config.TextColumn(width="small"),
    }, on_select="rerun", selection_mode="single-row", key="catalog_table")
    st.caption("点击商品行，可查看该商品的价格变更记录。")
    current_catalog_fingerprint = tuple(row["product_key"] for row in shown)
    previous_catalog_fingerprint = st.session_state.get("catalog_fingerprint")
    st.session_state["catalog_fingerprint"] = current_catalog_fingerprint
    selected_indices = catalog_event.selection.rows if previous_catalog_fingerprint == current_catalog_fingerprint else []
    if selected_indices and selected_indices[0] < len(shown):
        selected_product = shown[selected_indices[0]]
        with st.expander(f"{selected_product['product_name']} · 价格变更记录", expanded=True):
            if role == "admin" and st.button(
                "从商品列表移除并停止后续统计",
                key=f"exclude_selected_{selected_product['product_key']}",
            ):
                exclude_products(selected_product["member_keys"])
                st.session_state.pop("price_backup_data", None)
                st.session_state["price_flash"] = f"已将「{selected_product['product_name']}」从列表移除，可在下方恢复。"
                st.rerun()
            product_history = list_product_history(
                selected_product["product_key"], selected_product["member_keys"]
            )
            if product_history:
                detail_df = pd.DataFrame([{
                    "更新日期": display_date(row["updated_at"]),
                    "鱼商报价(¥)": row["supplier_quote_jpy"],
                    "更新来源": row["source"],
                } for row in product_history])
                st.dataframe(detail_df, use_container_width=True, hide_index=True, height=table_height(len(detail_df)), column_config={
                    "更新日期": st.column_config.TextColumn(width="small"),
                    "鱼商报价(¥)": st.column_config.NumberColumn(width="small", format="¥%.2f"),
                    "更新来源": st.column_config.TextColumn(width="small"),
                })
            else:
                st.caption("暂时没有这件商品的价格变更记录。")

else:
    st.info("价格库目前为空。管理员可在左侧迁移本机价格库，或上传当天价格表。")

if role == "admin":
    with st.expander("恢复已移除的品种", expanded=False):
        excluded = list_excluded_products()
        if excluded:
            excluded_choices = {row["product_key"]: row["product_name"] for row in excluded}
            restore_keys = st.multiselect(
                "已忽略品种（可选择恢复）", options=list(excluded_choices),
                format_func=lambda key: excluded_choices[key], key="restore_products_choice",
            )
            if st.button("恢复所选品种", key="restore_products_button", disabled=not restore_keys):
                restore_excluded_products(restore_keys)
                st.session_state.pop("price_backup_data", None)
                st.session_state["price_flash"] = "已恢复所选品种；后续价格表会重新更新它们。"
                st.rerun()
