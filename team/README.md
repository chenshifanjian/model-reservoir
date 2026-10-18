# 组队协调服务（自建指南）

「模型蓄水池」的组队功能需要一台**公网可达的协调服务器**。它的唯一职责是当一个
**加密信箱**：存队伍状态、成员表、以及队长推来的**密文**密钥包。

> **服务器看不到任何密钥。** 密钥在客户端用组队码派生的密钥加密后才上传，
> 服务端代码里根本没有解密能力（`coordinator.py` 不 import `cryptography`）。

你可以用本仓库提供的默认服务器，也可以自己搭一个。自己搭只需一台 512MB 内存的小机器。

---

## 一、它做什么 / 不做什么

| 做 | 不做 |
|---|---|
| 建队 / 生成 6 位组队码 | 不解密任何密文 |
| 队员输码入队、队长审批 | 不转发模型对话（模型调用直连上游） |
| 存队长推来的**密文**密钥包，按成员分发 | 不限制用量、不审计内容 |
| 统计上报的用量数字（时间/模型/token 数） | 不持有任何上游 API key |
| 关队、踢人、队伍过期清理 | — |

模型的实际调用**不经过这台服务器**：队员解密出上游配置后，是直连上游 API 的。
协调服务器挂了，已经同步过的队伍照样能用，只是不能再审批新成员或改昵称。

---

## 二、部署（一条命令）

需要：一台有公网 IP 的 Linux、root 权限、`python3`、`cryptography` 包、`sshpass`（在你本机跑脚本用）。

```bash
# 在你本机跑，脚本会通过 SSH 把服务推到目标机
SSHPASS='<目标机root口令>' TEAM_HOST='<你的服务器IP>' bash team/deploy.sh
```

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TEAM_HOST` | 必填 | 目标机 IP 或域名 |
| `TEAM_SSH_PORT` | `22` | SSH 端口 |
| `TEAM_PORT` | `5022` | 协调服务监听端口 |
| `TEAM_DIR` | `/opt/reservoir-team` | 远端安装目录 |

脚本是**幂等**的：重复运行只覆盖代码并重启服务，数据库不动。
装完会自动做健康检查、代码版本自检、内存占用确认。

如果目标机没有 `cryptography`：

```bash
python3 -m pip install --break-system-packages cryptography
```

---

## 三、套上 HTTPS（必须）

协调服务只监听 `127.0.0.1`，不直接暴露公网。请在前面加一层 nginx 反代并终结 TLS。

参考配置在 [`nginx-model-reservoir.conf`](./nginx-model-reservoir.conf)。

> ⚠️ **两个必踩的坑**（本项目实际踩过）：
>
> **① 上游协议必须写 `http://`，不是 `https://`**
> TLS 在 nginx 这层终结，后端是明文 HTTP。写成 `https://127.0.0.1:5022` 会得到
> 「回环 200、公网 502」的诡异现象 —— 症状是服务明明活着、本地 curl 正常，
> 但外网访问全部 502。判断依据：nginx error log 里 `upstream: "https://..."`。
> 如果非要写 https，还得额外去掉 `proxy_ssl_server_name`。
>
> **② 如果用 1Panel，它的配置是「双写」的**
> 同一份配置同时存在文件系统 `<站点目录>/proxy/*.conf` 和面板数据库里。
> 只改文件系统的话，下次在面板点保存会被覆盖回去。
> 所以建站点请走面板；手工改文件只适合临时调试。改完记得 `nginx -t` 再 reload。

验证：

```bash
curl -s https://<你的域名>/health
# 期望：{"ok": true, "service": "reservoir-team", "ts": ...}
```

---

## 四、让客户端连上你的服务器

客户端的「组队」页里有一个**协调服务器**输入框，默认填的是本项目作者维护的服务器。
把它改成你自己的地址，回车即可。

改完后客户端会保存这个设置，之后建队/入队都走你的服务器。

---

## 五、systemd 加固

[`reservoir-team.service`](./reservoir-team.service) 已经带了这些加固（可直接用）：

| 项 | 值 | 为什么 |
|---|---|---|
| `MemoryMax` | `128M` | 服务实际只吃 ~15MiB；超了被 OOM 杀掉重启，而不是拖垮同机其他服务 |
| `ProtectSystem` | `strict` | 只能写自己的数据目录 |
| `NoNewPrivileges` | `true` | 它不需要任何特权 |
| `PrivateTmp` / `PrivateDevices` | `true` | 隔离 |
| `Restart` | `on-failure` | 崩了自愈 |

数据落在 `team.db`（SQLite，WAL 模式）。备份就拷这一个文件。

---

## 六、HTTP API

全部 JSON。`Authorization: Bearer <token>`，token 是建队/入队时由服务端下发的成员令牌。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查（免鉴权） |
| `POST` | `/api/teams` | 建队。body: `captain_client_id`, `code_hash`, `code_salt`, `ttl`, `nickname` → 返回 `team_id` + 成员 `token` |
| `POST` | `/api/teams/join` | 输码入队。body: `team_id`, `code_hash`, `client_id`, `nickname` → 返回 `code_salt`（客户端据此派生密钥）+ 成员 `token` |
| `POST` | `/api/teams/<id>/approve` | 队长批准成员（仅队长） |
| `POST` | `/api/teams/<id>/kick` | 队长移出成员（仅队长） |
| `POST` | `/api/teams/<id>/disband` | 队长关队（仅队长）。之后码立即失效，队员自动置灰 |
| `POST` | `/api/teams/<id>/nickname` | 改自己昵称 |
| `POST` | `/api/teams/<id>/push_packages` | 队长推**密文**密钥包（仅队长）。body: `[{provider_name, nonce, payload}]` |
| `GET` | `/api/teams/<id>/packages` | 队员拉自己的密文包 |
| `POST` | `/api/teams/<id>/usage` | 上报用量数字（不含对话内容） |
| `GET` | `/api/teams/<id>/state` | 拉队伍状态 + 成员表 + 自己是否还被认可 |
| `GET` | `/api/teams/<id>/usage` | 队长看分人/分模型统计（仅队长） |

### 限制（防滥用）

| 项 | 值 |
|---|---|
| 请求体上限 | 1MB |
| 队伍默认有效期 | 7 天（上限 30 天），到期自动失效 |
| 错码尝试 | 每队每 IP 5 次，锁 3600 秒 |
| 每成员最多收包 | 64 |
| 单次用量上报 | 200 条 |
| 昵称长度 | ≤ 32 字符 |

### 安全模型

- **密钥派生**：`K_team = scrypt(组队码 ‖ team_id, salt, N=2^15, r=8, p=1)`，再 HKDF 按成员 ID 派生 `K_member`
- **服务端只存** `code_salt`（明文，用于派生）和 `code_hash`（scrypt 结果，用于验码）
- **服务端不存** `K_team` / `K_member`，**也不需要** —— 它只搬运密文
- 离线暴力破解 6 位码的成本：6 位数字 = 100 万种，每种要跑一次 scrypt（N=2^15 约 145ms），
  单核约 40 小时；多核并行可加速，但盐是每队随机的，无法跨队预计算
- 队伍 TTL 7 天，配合防离线枚举

---

## 七、故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 公网 502，回环 200 | 反代上游协议写成了 `https://` | 改成 `http://`，reload |
| 客户端拿不到 `code_salt` | 跑的是旧版 coordinator | `systemctl restart reservoir-team`（`enable --now` 对已运行进程无效） |
| 队员拉包为空 | 队长没勾选该上游 / 队伍已关 | 队长检查「同步」勾选；确认队伍状态 |
| 服务起不来 | 端口被占 / 缺 cryptography | `journalctl -u reservoir-team -n 20 --no-pager` |
| 内存被 OOM 杀 | 上游反代配置错误导致堆积 | 看 journal；`MemoryMax=128M` 是保险丝 |

日志：`journalctl -u reservoir-team -f`
