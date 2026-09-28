# Trade81 海鲜价格库

支持本机 SQLite 与 Supabase PostgreSQL 共享数据库、Google 登录、管理员 / 只读权限，以及商品归并。价格以日元保存。

## 本机启动

在程序目录打开命令提示符：

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

以后启动：

```bat
.venv\Scripts\activate
python -m streamlit run app.py
```

本机数据保存在 `%LOCALAPPDATA%\Trade81FishPrice\prices.db`。

## 每日价格更新

管理员在应用左侧“更新当天价格”上传当天最新价格表即可，不需要再上传前一天的表，也不需要单独上传下单表。系统会：

- 更新文件中出现的商品价格；文件未出现的商品保留原价格。
- 按每个商品行出现次数累计下单次数；同一天重传会替换该日计数，不会重复加总。
- 检查同一商品是否出现多个报价；有冲突的商品不会自动改价，其他商品照常更新。

## 手动归并商品名称

在左侧“归并相同品种”中，从已有品种列表选中两项或多项，再输入归并后的显示名。例如选中 `みかん鯛 1.8kg` 和 `養殖ミカンタイ みかん鯛 1.8 kg 1`，合并后可命名为 `みかん鯛`。

保存后，所选商品的价格、下单次数和更新记录会合并显示在主要品名下。取消归并会恢复原始商品的分别显示；原始价格和历史记录不会删除。以大写 `A` 开头的品项会自动略过。

主表精简显示品种、各商品下单次数、鱼商报价和更新日期；更新日期显示为“26年9月18日”格式。海胆等大写 A 开头的商品不会进入价格页或下单统计。

## 云端配置

Streamlit Community Cloud 应用需在 App settings → Secrets 配置：

- `SUPABASE_DB_URL`：Supabase PostgreSQL 连接 URI。
- `[auth]`：Google OAuth Client ID、Client Secret、随机 cookie secret 和部署后的 `redirect_uri`。
- `[access]`：管理员邮箱放入 `admin_emails`，同事邮箱放入 `viewer_emails`。

参考 `.streamlit/secrets.toml.example`。真实连接 URI、数据库密码、OAuth 密钥和 `secrets.toml` 不要提交到 GitHub。

Google Auth Platform 处于 Testing 时，需要把每个登录账号添加到测试用户。部署时 Google OAuth 的 Authorized redirect URI 应为：

```text
https://YOUR-APP.streamlit.app/oauth2callback
```

## 本机数据迁移

先在本机应用左侧下载“价格库备份”，再用云端管理员账号上传迁移。迁移只允许写入空数据库，不会覆盖云端已有数据。备份包含当前价格、更新历史、下单统计和品名归并规则。

## 依赖与安全

代码仓库在 Streamlit Community Cloud 免费方案下需要公开；页面仍要求 Google 登录。数据库和 OAuth 凭据只保存在部署 Secrets 中。不要上传 `.venv`、价格数据库或任何真实密钥。
