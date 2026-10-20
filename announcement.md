# 模型蓄水池 · Model Reservoir v2.1.2

一次只读审计逐条复核后，修掉 **7 类缺陷** —— 其中两个能让网关全路由 500 且**重启依旧**。

## 一、坏状态会被持久化（重启不自愈）

这是这版里最贵的两类。

### routers.json 顶层是 `null` 或数组

`null` 是**合法 JSON**，所以以前只 catch `JSONDecodeError` 的写法接不住它 ——
它直接把 `None` 返回出去，于是 `ROUTERS[model]` 抛异常，
`/v1/models` 与 `/v1/chat/completions` 全 500，重启依旧。

现在按 config/providers 同样的方式处理：类型不符就留档 + 退回空表，
并且逐条消毒（成员必须是字符串，非列表的组直接丢弃）。

### `POST /api/routers` 不校验 body

POST 一个 `null` 或 `{"p": 123}` 会返回 **200** 并写进磁盘，
之后该组名下所有请求 500。一个请求就能制造出无法自愈的坏状态。

现在落盘前校验，形状不对回 400 并指出**哪一组**坏了；
拒绝之后磁盘保持原样（有专门的测试盯这一点）。

### providers.json 顶层是对象

同理会让三个主接口全 500。加载时已判 `isinstance(list)`。

## 二、未捕获异常一律回默认 500

全项目**没有任何 `exception_handler`**。上游返回畸形数据、客户端传错类型时，
OpenAI 客户端拿不到 `error` 对象、Anthropic 客户端拿不到 `error.type` ——
既看不出原因也无从重试。

现在按协议面分三种形状兜底：

| 路径 | 错误体形状 |
|---|---|
| `/v1/messages` | `{"type":"error","error":{"type":"api_error",…}}` |
| `/v1/*` | `{"error":{"message":…,"type":"server_error",…}}` |
| `/api/*` | `{"ok":false,"error":…}` |

完整堆栈写进日志；返回体只含异常类型与消息，**不含本地路径与密钥**。

顺带修掉四个具体触发点：

- 识图分支 `body.get(key, 0) > 16384` —— `max_tokens` 传 `"99999"` 或 `null` 直接 TypeError
- `_inject_cn_hint` 的 `if CN_HINT not in t` —— text 块是数字就炸
- Anthropic 翻译 `to_openai_request` 在 try **之外** —— 该处下方的注释原本就写着
  「不能让客户端拿到非 Anthropic 形状的体」，只是漏了自己上面这一行
- `PUT /api/port` 传 `"abc"` → 500，现在 400

## 三、模型列表缓存失效点

以前唯一失效点在「改模型能力」那条路上 → 增删上游、改禁用列表后，
30 秒内 `GET /v1/models` 仍返回已删掉的模型，客户端选中它就直接失败。

现在把失效搬进 `save_providers()` **内部** —— 六个写路径全覆盖，
将来新加的写点也漏不掉。

## 四、写盘强度

`atomic_write` 以前不 fsync、tmp 名固定。断电时 rename 可能先于数据落盘：
文件名已换、内容还是空的 —— 这正是 config/providers 变成半截 JSON 的来源之一。

现在写完 `flush()+fsync()` 再 rename，tmp 名带 pid 防两个进程互踩。

## 验证

- 269 单元测试（新增 18 例）
- 真实起服务实测：四个非法 routers 请求全 400、磁盘内容仍合法、
  `PUT /api/port` 传非数字 400、providers 损坏后重启存活并留档
- **一条测试当场抓出我自己新写代码里的 bug**：消毒时把判断写成了列表而不是成员，
  会把 `1`/`null` 变成字符串模型名放进来

> v2.1.0 / v2.1.1 的内容（体积 169MB→96MB、移除 Ollama / LM Studio、
> 启动不再静默失败、三个静默崩溃路径）见上两个版本的发布说明。
