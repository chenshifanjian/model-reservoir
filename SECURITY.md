# 安全说明 · Security

这个项目处理的是**各家 LLM 提供商的 API Key** —— 它们是真金白银的凭据。
本文说明它的安全模型、已知边界、以及你自己部署时该注意什么。

---

## 一、数据分类

| 数据 | 存哪 | 谁能看到 |
|---|---|---|
| 上游 API Key | 本机 `dist/providers.json`（明文） | 只有你本机 |
| 本机网关 key | 本机 `dist/config.json` | 只有你本机 |
| 调用记录 / 用量 | 本机 `history.jsonl` / `usage.jsonl` | 只有你本机 |
| 对话内容 | **本项目不存储** | — |
| 组队密钥包（密文） | 协调服务器 `team.db` | 只有你的队员（他们本来就有明文） |
| 队伍状态 / 成员表 / 用量数字 | 协调服务器 `team.db` | 队长 |

**模型对话不经过协调服务器。** 协调服务器只在建队/审批/同步密钥时被用到；
队员解密出上游配置后，调用模型是直连上游的。

---

## 二、上游 Key 的保护

**明文存储是设计使然**：网关要拿 Key 去调上游，不存明文就得你每次输入。
这是所有同类聚合工具（以及各家 SDK）的共同做法。

因此：

- 仓库里 `providers.json` / `config.json` **已进 `.gitignore`**，不会被提交
- 提供了 `providers.example.json` / `config.example.json` 作为模板（无真实凭据）
- 本仓库发布前做过脱敏扫描：`git grep -nEI 'sk-[A-Za-z0-9_-]{16,}'` 对源码无命中
- 网关只监听 `127.0.0.1`，不直接暴露公网
- 访问本机 API 需要 `Authorization: Bearer <local_api_key>`

**你自己要做的**：别把 `dist/` 整个打包发给别人；别在公开 issue 里贴 `providers.json`。

---

## 三、组队的密钥模型

这是本项目唯一涉及「密钥离开本机」的路径，所以多说几句。

### 密钥派生

```
K_team   = scrypt(组队码 ‖ team_id, salt, N=2^15, r=8, p=1)   # dklen=32
K_member = HKDF(K_team, salt=None, info=member_id, dklen=32)
```

- `salt` 由服务端生成、每队随机 32 字节，建队/入队时下发给客户端
- `K_team` 对所有成员相同；`K_member` 按成员 ID 派生，成员间互相解不开对方的包
- 加密用 AESGCM-256，nonce 随机 12 字节

### 服务端能看到什么

| 有 | 无 |
|---|---|
| `code_salt`（明文，因为客户端要用它派生密钥） | `组队码`（只存 `scrypt(code, salt)` 的结果） |
| `code_hash`（验码用） | `K_team` / `K_member` |
| 密文包（nonce + payload） | 任何明文密钥 |
| 用量数字（时间/模型/token 数） | 对话内容 |

`team/coordinator.py` **不 import `cryptography`** —— 它在代码层面就没有解密能力。

### 6 位码能被暴力破解吗

6 位数字 = 100 万种。每次尝试要跑一次 scrypt（N=2^15，约 145ms/次），
单核约 40 核·小时；8 核约 5 小时。

**缓解措施**：

1. 每队随机 `salt` → 无法跨队预计算 rainbow table
2. 队伍默认 **7 天 TTL**，过期即失效
3. 服务端每队每 IP **5 次错码锁定 3600 秒**

**结论**：能抢在 7 天内、用足够多算力攻破**单个队伍**。
但攻击者得先知道 `team_id`（32 字节随机 hex）。
**所以：不要把组队码长期公开挂在论坛/群里；临时组队、用完就关队。**

### 同步即复制，不可收回

队长勾选同步后，密钥会被加密推到服务器、队员拉取解密后**写进他自己的配置文件**。
此后：

- **无法远程收回**。你能做的只有关队（之后他那边的上游会被置灰、模型全禁用），
  但密钥本身已经在他机器上。
- 真正的补救是**你自己去上游控制台换 Key**。

客户端在每个危险操作上都会明确提示这一点。

---

## 四、协调服务器的加固

如果自建，[`team/README.md`](./team/README.md) 里有部署脚本和 systemd 单元，
已包含这些加固：

| 项 | 值 |
|---|---|
| 监听 | `127.0.0.1`（不直接暴露公网，靠 nginx 反代） |
| `MemoryMax` | `128M`（实际只吃 ~15MiB；超了被 OOM 杀重启，不拖垮同机其他服务） |
| `ProtectSystem` | `strict`（只能写自己的数据目录） |
| `NoNewPrivileges` | `true` |
| 请求体上限 | 1MB |
| 错码限流 | 每队每 IP 5 次 / 3600 秒 |

---

## 五、前端

- 网页在 pywebview 的 WebKitGTK 里渲染，只加载本机内容，**不加载外部脚本/CDP**
- 所有拼进 `innerHTML` 的动态文本（队友昵称、上游名、模型名、服务端错误串）
  都经 `escHtml` / `escTeam` 转义
- **不使用 `confirm()` / `prompt()` / `alert()`** —— 这些在 WebKitGTK 下会挂起 JS 线程
- 不使用的模态一律关闭（`dom_blinking=false`、`media_stream=false`、
  `allow_modal_dialogs=false`）

---

## 六、许可证与授权

许可证 **CC BY-NC 4.0**（署名 + 非商业），沿用原仓库。
2026-10-03 原作者（东野 / ywtc000）已书面授权本 fork 二次开发并开源。

**如果你要用它做商业服务**（比如给客户收费提供聚合 API），需要先取得原作者授权。

---

## 七、报告问题

发现安全问题请开 **private security advisory**（GitHub → Security → Advisories），
不要直接开公开 issue —— 这个项目涉及真实 API Key。

我会尽量及时响应。已知且可接受的设计取舍（见上文）：上游 Key 明文存储、
6 位组队码的搜索空间、同步后不可远程收回。
