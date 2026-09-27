# Trade81 海鲜价格库：免费共享版准备包

此版本保留本机 SQLite 模式，也支持 Supabase PostgreSQL 共享数据库、Google 登录、管理员 / 只读权限和本机数据迁移。

## 本机启动

在程序目录打开命令窗口：

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

Windows 本机数据仍保存在 `%LOCALAPPDATA%\Trade81FishPrice\prices.db`。

## 免费云端试运行的顺序

### 1. 上传程序到 GitHub 私有仓库

创建一个 **Private repository**，上传此文件夹中的程序文件。不要上传 `.venv`、价格数据库文件或真实的 `secrets.toml`。`.gitignore` 已排除这些内容。

### 2. 创建 Supabase 免费项目

在 Supabase 建立一个 Free 项目，然后从数据库连接设置复制 PostgreSQL 连接 URI。给 Streamlit 使用时选择 Session Pooler URI；保管好数据库密码。

### 3. 部署到 Streamlit Community Cloud

使用 GitHub 账号登录 Streamlit Community Cloud，选择私有仓库，部署 `app.py`。先保持应用为 private。首次部署后会显示数据库未配置，这是预期状态，下一步添加 Secrets 后即可连接。

### 4. 在 Streamlit 的 Secrets 中填入配置

在 App settings → Secrets 中，按 `.streamlit/secrets.toml.example` 填入：

- `SUPABASE_DB_URL`：Supabase 的 PostgreSQL URI。
- `[auth]`：Google OAuth Client ID、Client Secret、随机 cookie secret，以及部署后的 `redirect_uri`。
- `[access]`：你自己的邮箱放在 `admin_emails`；同事邮箱放在 `viewer_emails`。

不要把真实密码、Client Secret 或连接 URI 提交到 GitHub，也不要发到聊天中。

### 5. 配置 Google 登录

在 Google Auth Platform 创建一个 Web application OAuth Client。在 Authorized redirect URIs 加入：

```text
https://YOUR-APP.streamlit.app/oauth2callback
```

将 Client ID 和 Client Secret 放入 Streamlit Secrets。Google OAuth 处于 Testing 时，需要把登录账号加到测试用户；准备让同事登录时，再按 Google 页面要求发布登录同意屏幕。

### 6. 邀请同事

在 Streamlit app 的 Share / Settings 中将同事邮箱加入 private viewers；同时将他们的 Google 邮箱加入 Secrets 的 `viewer_emails`。两处都需要允许。管理员邮箱只放进 `admin_emails`，因此只有管理员可以上传价格表和手动修改。

### 7. 迁移本机价格

在当前本机程序左侧点“下载价格库备份 / 迁移文件”。登录云端管理员账号，在右侧“从本机迁移价格库”上传该文件。迁移只允许写入空的云端价格库，以免覆盖已共享数据。

## 免费方案的限制

- Streamlit Community Cloud 的应用连续 12 小时没有访问会休眠；有人打开后可以唤醒。
- Supabase Free 项目连续一周无活动会暂停。该方案适合试运行，长期业务使用前要考虑备份与可用性。
- 私有 Streamlit app 只能邀请特定邮箱查看。应用里还会再用 Google 登录名单区分管理员和只读同事。
