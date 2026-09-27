from __future__ import annotations

import os
import pandas as pd
import streamlit as st

from price_parser import group_price_rows, read_price_template
from price_store import (
    apply_sheet, configure_database, export_backup_xlsx, import_backup_xlsx,
    list_history, list_prices, manual_update,
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
    try:
        return value if value else {}
    except Exception:
        return {}


database_url = secret("SUPABASE_DB_URL") or os.getenv("SUPABASE_DB_URL")
configure_database(database_url)
local_mode = bool(os.getenv("LOCALAPPDATA") or os.getenv("TRADE81_LOCAL_MODE") == "1")

st.title("🐟 Trade81 海鲜价格库")
st.caption("共享价格按日元显示。实际进货成本 = 鱼商报价 × 1.05。")

if not database_url and not local_mode:
    st.error("共享数据库尚未配置。请在部署平台的 Secrets 中设置 SUPABASE_DB_URL。")
    st.stop()

auth_config = secret_section("auth")
access_config = secret_section("access")
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

user_col, logout_col = st.columns([5, 1])
user_col.caption(f"当前账号：{email} · {'管理员' if role == 'admin' else '只读'}")
if auth_config and logout_col.button("退出登录"):
    st.logout()

try:
    current = list_prices()
except Exception as exc:
    st.error(f"无法连接价格数据库：{exc}")
    st.stop()

left, right = st.columns([1.05, 1.55], gap="large")

with left:
    st.subheader("已知海鲜价格")
    if current:
        search = st.text_input("搜索品种 / Size", key="catalog_search", placeholder="例如：ハマチ、350g")
        shown = [row for row in current if not search or search.lower() in row["product_name"].lower()]
        catalog_df = pd.DataFrame([{
            "品种 / Size": row["product_name"],
            "鱼商报价(¥)": row["supplier_quote_jpy"],
            "加 5% 后(¥)": round(row["supplier_quote_jpy"] * (1 + MARKUP), 2),
            "价格表日期": row["source_date"],
            "最近更新时间": row["updated_at"],
        } for row in shown])
        st.dataframe(catalog_df, use_container_width=True, hide_index=True, height=380,
                     column_config={
                         "鱼商报价(¥)": st.column_config.NumberColumn(format="¥%.2f"),
                         "加 5% 后(¥)": st.column_config.NumberColumn(format="¥%.2f"),
                     })
        if role == "admin":
            st.markdown("**手动修改鱼商报价**")
            by_key = {row["product_key"]: row for row in current}
            selected = st.selectbox(
                "选择商品", list(by_key),
                format_func=lambda key: by_key[key]["product_name"],
                key="manual_product",
            )
            selected_row = by_key[selected]
            with st.form("manual_price_edit"):
                edited_quote = st.number_input(
                    "鱼商报价（¥）", min_value=0.0, value=float(selected_row["supplier_quote_jpy"]),
                    step=100.0, key=f"quote_{selected}",
                )
                st.caption("修改的是鱼商报价；实际进货成本会自动加 5%。日期更新的价格表会覆盖手动值。")
                save_manual = st.form_submit_button("保存手动价格")
            if save_manual:
                manual_update(selected, edited_quote, updated_by=email)
                st.session_state["price_flash"] = "已保存手动价格。"
                st.rerun()
    else:
        st.info("价格库目前为空。管理员可以在右侧上传价格模板，或迁移本机价格库。")

    if role == "admin" and current:
        st.download_button(
            "下载价格库备份 / 迁移文件",
            data=export_backup_xlsx(),
            file_name="Trade81_价格库备份.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    with st.expander("最近价格更新记录"):
        history = list_history(30)
        if history:
            history_df = pd.DataFrame([{
                "品种 / Size": row["product_name"],
                "鱼商报价(¥)": row["supplier_quote_jpy"],
                "价格表日期": row["source_date"],
                "更新时间": row["updated_at"],
                "来源": row["source"],
                "操作人": row.get("updated_by", ""),
            } for row in history])
            st.dataframe(history_df, use_container_width=True, hide_index=True)
        else:
            st.caption("还没有价格更新记录。")

with right:
    st.subheader("共享价格更新")
    if role == "admin":
        if database_url and not current:
            with st.expander("从本机迁移价格库"):
                st.caption("先在本机版下载“价格库备份 / 迁移文件”，再上传到这里。只能导入到空价格库。")
                backup_file = st.file_uploader("本机价格库备份（.xlsx）", type=["xlsx"], key="migration_backup")
                if st.button("导入本机价格与历史", disabled=backup_file is None):
                    try:
                        count = import_backup_xlsx(backup_file)
                        st.session_state["price_flash"] = f"已迁移 {count} 条商品价格及更新记录。"
                        st.rerun()
                    except Exception as exc:
                        st.error(f"迁移失败：{exc}")

        st.caption("上传两天的价格模板。餐厅和箱号不会参与匹配；新表缺少的商品沿用已知价格。")
        old_file = st.file_uploader("① 较早一天的价格模板（.xlsx）", type=["xlsx"], key="older_price")
        new_file = st.file_uploader("② 较新一天的价格模板（.xlsx）", type=["xlsx"], key="newer_price")

        if st.button("比对并更新共享价格库", type="primary", disabled=not (old_file and new_file)):
            st.session_state.pop("price_compare_report", None)
            try:
                old_file.seek(0)
                new_file.seek(0)
                older = read_price_template(old_file)
                newer = read_price_template(new_file)
                if not older["date"] or not newer["date"]:
                    st.error("无法从模板标题读取日期，请确认价格模板第 5 行包含日期。")
                elif older["date"] >= newer["date"]:
                    st.error(f"日期顺序不正确：较早模板是 {older['date']}，较新模板是 {newer['date']}。请调整上传位置。")
                else:
                    old_map, old_conflicts = group_price_rows(older)
                    new_map, new_conflicts = group_price_rows(newer)
                    old_stats = apply_sheet({**older, "rows": [r for r in older["rows"] if r["key"] not in old_conflicts]}, updated_by=email)
                    new_stats = apply_sheet({**newer, "rows": [r for r in newer["rows"] if r["key"] not in new_conflicts]}, updated_by=email)
                    report = []
                    all_keys = set(old_map) | set(new_map) | set(old_conflicts) | set(new_conflicts)
                    for key in sorted(all_keys):
                        old_item = old_map.get(key)
                        new_item = new_map.get(key)
                        example = new_item or old_item or next(
                            (r for r in newer["rows"] + older["rows"] if r["key"] == key), None
                        )
                        if example is None:
                            continue
                        if key in new_conflicts:
                            status = "新表同品种有多个报价，未自动更新"
                            new_quote = None
                        elif new_item and old_item:
                            status = "价格有变化" if new_item["fishmonger_quote_jpy"] != old_item["fishmonger_quote_jpy"] else "价格相同"
                            new_quote = new_item["fishmonger_quote_jpy"]
                        elif new_item:
                            status = "新表价格（旧表有多个报价）" if key in old_conflicts else "新表新增品种"
                            new_quote = new_item["fishmonger_quote_jpy"]
                        else:
                            status = "新表未出现，沿用较早价格"
                            new_quote = old_item["fishmonger_quote_jpy"] if old_item else None
                        old_quote = old_item["fishmonger_quote_jpy"] if old_item else None
                        report.append({
                            "品种 / Size": example["canonical_name"],
                            "较早价格(¥)": old_quote,
                            "较新价格(¥)": new_quote,
                            "差额(¥)": round(new_quote - old_quote, 2) if old_quote is not None and new_quote is not None else None,
                            "处理结果": status,
                            "key": key,
                        })
                    st.session_state["price_compare_report"] = {
                        "older_date": older["date"], "newer_date": newer["date"],
                        "rows": report, "older_stats": old_stats, "newer_stats": new_stats,
                        "old_conflicts": old_conflicts, "new_conflicts": new_conflicts,
                    }
                    st.session_state["price_flash"] = (
                        f"已比较 {older['date']} 与 {newer['date']}："
                        f"较早表更新 {old_stats['changed']} 项，较新表更新 {new_stats['changed']} 项。"
                    )
                    st.rerun()
            except Exception as exc:
                st.error(f"读取或更新失败：{exc}")
    else:
        st.info("你目前有只读权限。请联系价格库管理员上传价格模板或修改价格。")

    report = st.session_state.get("price_compare_report")
    if report:
        st.markdown(f"**比较结果：{report['older_date']} → {report['newer_date']}**")
        visible_rows = [{k: v for k, v in row.items() if k != "key"} for row in report["rows"]]
        st.dataframe(pd.DataFrame(visible_rows), use_container_width=True, hide_index=True,
                     column_config={
                         "较早价格(¥)": st.column_config.NumberColumn(format="¥%.2f"),
                         "较新价格(¥)": st.column_config.NumberColumn(format="¥%.2f"),
                         "差额(¥)": st.column_config.NumberColumn(format="¥%.2f"),
                     })
        if report["new_conflicts"]:
            conflict_rows = [{
                "品种 / Size": key,
                "发现报价(¥)": " / ".join(str(v) for v in values),
            } for key, values in report["new_conflicts"].items()]
            st.warning("新价格表有同规格的不同报价，相关项目没有自动更新，请先核对。")
            st.dataframe(pd.DataFrame(conflict_rows), use_container_width=True, hide_index=True)

with st.expander("价格规则与免费版说明"):
    st.markdown("鱼商报价统一加 5% 作为实际进货成本；全部按日元，不做汇率换算。云端共享数据存于 Supabase；免费数据库若一周无活动会暂停。Streamlit Community Cloud 应用若 12 小时无人访问会休眠。")
