# VTranslate 服务端部署

服务端 = 计费 API + 实时翻译中转（WebSocket）+ 管理后台（`/admin/`），单个 Python 进程。
客户端（Electron 桌面端）在「设置 → 运行模式 → 计费服务器」里填 `https://你的域名` 即可连接。

## 1. 打包

在开发机（Windows）仓库根目录执行：

```bat
package_server.bat
```

产物：`dist\vtranslate-server.tar.gz`，上传到服务器并解压：

```bash
tar xzf vtranslate-server.tar.gz && cd vtranslate-server
```

## 2A. Docker 部署（推荐）

```bash
cp server/.env.example server/.env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成 ADMIN_TOKEN
vi server/.env        # 填 DASHSCOPE_API_KEY / ZPAY_* / ADMIN_TOKEN
cd deploy && docker compose up -d --build
curl http://127.0.0.1:8765/healthz
```

数据库持久化在 Docker 卷 `vt-data`（容器内 `/data/billing.db`）。
升级：上传新包解压覆盖后再次 `docker compose up -d --build`。

## 2B. 裸机部署（systemd）

```bash
sudo bash deploy/install.sh     # 首次会生成 /opt/vtranslate/server/.env 和随机 ADMIN_TOKEN
sudo vi /opt/vtranslate/server/.env
sudo systemctl restart vtranslate
```

数据库在 `/var/lib/vtranslate/billing.db`。升级：解压新包后再次执行 `install.sh`（保留 .env 和数据）。

## 3. Nginx + HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/conf.d/vtranslate.conf
sudo sed -i 's/api.example.com/你的域名/g' /etc/nginx/conf.d/vtranslate.conf
sudo certbot --nginx -d 你的域名        # 签发证书
sudo nginx -t && sudo systemctl reload nginx
```

`.env` 里 `ZPAY_NOTIFY_URL` 设为 `https://你的域名/api/pay/notify`。

## 4. 管理后台

打开 `https://你的域名/admin/`，用 `ADMIN_TOKEN` 登录。功能：

| 页面 | 作用 |
| --- | --- |
| 概览 | 今日/累计收入、翻译时长、在线会话、14 天趋势、上游/支付配置状态 |
| 用户 | 搜索、加减余额/时长卡/订阅、设定免费额度、备注、封禁、强制下线、重置密码 |
| 订单 | 筛选、向支付网关查单、回调丢失时手动补单、关闭订单 |
| 实时会话 | 正在翻译的连接（5 秒刷新），可单独断开 |
| 用量流水 | 每次扣费明细 |
| 术语热词 | 在线增删 / 批量导入游戏术语，直接注入翻译模型，提升准确度 |
| 运营设置 | 公告、试用额度、单价、返利、游客上限、断句静音、注册/游客开关 |

安全建议：`ADMIN_TOKEN` 用 32 位以上随机串；在 `nginx.conf` 的 `/admin` 和 `/api/admin/` 段启用 `allow 你的IP; deny all;`。

## 注意

- 只能单进程运行（SQLite + 进程内锁/限流），不要加 `--workers`，也不要多实例共用一个数据库文件。
- 备份：定期复制 `billing.db`（Docker：`docker cp vtranslate:/data/billing.db ./backup.db`）。
- 本地调试不需要 Key：`.env` 设 `MOCK_UPSTREAM=1`，返回模拟翻译结果。
