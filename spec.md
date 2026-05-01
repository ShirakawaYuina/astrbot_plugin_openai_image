# AstrBot 插件 Spec：OpenAI 图片生成与编辑插件
**版本**：v1.1  
**日期**：2026-04-22  
**插件名**：`astrbot_plugin_openai_image`

---

> 维护备注：英文扩写配置与命令尾部 `&` 触发逻辑已移除，`&` 现在按普通提示词字符处理。

## 1. 概述

本插件为 AstrBot 扩展插件，面向兼容 `chat/completions` 风格图片输出协议的图片网关，提供以下核心能力：

- 文本生成图片：`/oaiimg`
- 基于图片编辑图片：`/oaiedit`
- 将最终图片结果通过 OneBot v11 平台返回到 QQ 消息中

插件通过可配置的 `base_url`、`model`、`api_key` 调用外部图片接口。  
图片生成与图片编辑使用不同命令触发，图片缓存保存到 AstrBot 标准插件数据目录：

`data/plugin_data/astrbot_plugin_openai_image/images/`

本次规格在原方案基础上做以下关键调整：

- 图片命令由 `/opimg`、`/opedit` 调整为 `/oaiimg`、`/oaiedit`
- 已移除原英文扩写功能与相关配置项

---

## 2. 目标与范围

### 2.1 本期目标

- 支持配置图片接口 `base_url`
- 支持配置图片模型 `model`
- 支持配置图片接口认证 `api_key`
- 支持配置全局负面提示词
- 支持文本生成图片
- 支持图片编辑
- 支持编辑命令从“同条消息附图”或“回复/引用图片消息”两种方式读取输入图片
- 支持单次命令指定生成数量
- 支持插件级最大并发数量限制
- 支持插件级最大缓存图片数量限制
- 支持通过 OneBot v11 将结果图片回传到 QQ 聊天消息中

### 2.2 不在本期范围

- 不支持多图片同时编辑
- 不支持多供应商兜底链路
- 不支持流式返回
- 不支持与图片模型分离的独立翻译接口配置项

---

## 3. 需求结论

以下内容已根据需求确认，作为实现约束：

- 插件目录固定放在 `data/plugins/astrbot_plugin_openai_image/`
- 缓存目录固定放在 `data/plugin_data/astrbot_plugin_openai_image/images/`
- 图片生成命令使用 `/oaiimg`
- 图片编辑命令使用 `/oaiedit`
- 图片编辑同时支持：
  - 同一条消息带图执行命令
  - 回复/引用一条图片消息后执行命令
- 外部图片接口走 `chat/completions`
- 生成请求体中，`messages[0].content` 为字符串
- 编辑请求体中，`messages[0].content` 为数组，包含：
  - 文本块 `type=text`
  - 图片块 `type=image_url`
- 响应图片来自 `choices[0].message.content` 中的 Markdown 图片 `data:image/...;base64,...`
- 需要支持可配置最大并发数量
- 需要支持可配置最大缓存图片数量

---

## 4. 分层设计

本插件采用分层设计，避免将命令解析、业务编排、模型调用、缓存管理、消息回传堆叠在一个入口文件中。

### 4.1 分层结构

```text
命令层 -> 应用服务层 -> 图片网关层 -> 缓存存储层 -> 表达层
```

### 4.2 各层职责

#### 命令层

负责：

- 注册 AstrBot 命令
- 解析用户输入
- 提取数量参数与提示词
- 为编辑命令提取当前消息或引用消息中的图片
- 将结构化参数交给应用服务层

不负责：

- 不直接拼装 HTTP 请求
- 不直接解析外部接口响应
- 不直接管理缓存淘汰

#### 应用服务层

负责：

- 组织一次完整的图片生成或编辑业务流程
- 处理并发限制
- 处理批量任务调度
- 调用网关层与缓存层
- 汇总成功与失败结果

#### 图片网关层

负责：

- 构建生成请求
- 构建编辑请求
- 发送 HTTP 请求
- 解析 `chat/completions` 响应中的图片数据

#### 缓存存储层

负责：

- 解码图片并落盘
- 生成安全文件名
- 控制缓存数量
- 清理旧图片

#### 表达层

负责：

- 将结果图片组织成 AstrBot 消息链
- 通过 OneBot v11 将图片回传到 QQ
- 输出用户可读的成功、失败、部分成功提示

---

## 5. 目录结构设计

建议目录结构如下：

```text
data/plugins/astrbot_plugin_openai_image/
├── main.py
├── metadata.yaml
├── README.md
├── _conf_schema.json
├── requirements.txt
├── spec.md
├── core/
│   ├── __init__.py
│   ├── commands.py
│   ├── models.py
│   ├── services/
│   │   ├── __init__.py
│   │   ├── image_generate_service.py
│   │   ├── image_edit_service.py
│   │   └── image_task_service.py
│   ├── gateways/
│   │   ├── __init__.py
│   │   ├── openai_image_gateway.py
│   │   ├── request_builder.py
│   │   └── response_parser.py
│   ├── storage/
│   │   ├── __init__.py
│   │   ├── image_cache_store.py
│   │   └── cache_cleaner.py
│   ├── presenters/
│   │   ├── __init__.py
│   │   └── result_presenter.py
│   └── utils/
│       ├── __init__.py
│       ├── image_extract.py
│       ├── mime_utils.py
│       └── text_utils.py
└── tests/
    ├── test_request_builder.py
    ├── test_response_parser.py
    ├── test_image_extract.py
    ├── test_cache_cleaner.py
    └── test_task_service.py
```

说明：

- `main.py` 只保留插件入口、依赖组装、命令注册相关逻辑
- 复杂逻辑按职责拆到 `core/` 子模块
- 代码文件统一使用 UTF-8 编码

---

## 6. 命令设计

### 6.1 图片生成命令

```text
/oaiimg [数量] <提示词>
```

示例：

```text
/oaiimg 生成一只可爱的小狗图片
/oaiimg 3 赛博朋克城市夜景
```

规则说明：

- `数量` 为可选整数
- 未填写时默认生成 1 张
- `数量` 必须大于等于 1
- `数量` 不能绕过插件配置中的最大并发限制
- `&` 不具备特殊含义，会作为普通提示词字符传给图片接口

### 6.2 图片编辑命令

```text
/oaiedit [数量] <提示词>
```

示例：

```text
/oaiedit 改成电影感海报
/oaiedit 2 把人物改成动漫风格
```

规则说明：

- 与 `/oaiimg` 一样支持可选数量
- `/oaiedit` 必须检测到输入图片才允许执行
- 支持两种图片来源：
  - 当前消息附带图片
  - 回复/引用消息中的图片
- 若同时存在两种来源，优先使用“回复/引用消息中的图片”

### 6.3 错误提示

- 缺少提示词：提示正确命令格式
- `/oaiedit` 未检测到图片：提示“请在同一条消息中附图，或回复一张图片后再执行 /oaiedit”
- 数量不合法：提示数量必须为正整数
- 当前事件平台不是 OneBot v11：提示“当前插件首版仅支持通过 OneBot v11 返回 QQ 图片消息”

---

## 7. 配置设计

### 7.1 首版必做配置

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `base_url` | string | `https://api.jucode.cn/pg/chat/completions` | 图片接口地址 |
| `api_key` | string | `""` | 图片接口密钥 |
| `model` | string | `gpt-draw-1024x1536` | 图片模型名称 |
| `negative_prompt` | string | `""` | 全局负面提示词 |
| `request_timeout_seconds` | int | `180` | 图片请求超时时间 |
| `max_concurrency` | int | `2` | 插件最大并发任务数 |
| `max_cache_images` | int | `50` | 最大缓存图片数量 |

### 7.2 配置说明

- `base_url`、`api_key`、`model` 仅用于图片接口
- `negative_prompt` 为空时不修改用户提示词，非空时追加为 `Negative prompt: ...`
- 不再提供英文扩写模型选择项，插件不会调用 AstrBot 已配置模型做提示词改写

### 7.3 WebUI 行为要求

- 配置页中不再出现五项独立翻译配置
- 配置页不显示“英文扩写模型”或其他提示词改写相关配置项

---

## 8. 数据模型设计

### 8.1 命令解析结果

```python
@dataclass
class ParsedCommand:
    count: int
    prompt: str
```

### 8.2 图片编辑输入

```python
@dataclass
class EditInputImage:
    source: str
    mime_type: str
    raw_bytes: bytes
```

### 8.3 图片任务结果

```python
@dataclass
class ImageTaskResult:
    success: bool
    prompt: str
    image_path: str | None
    error_message: str | None
```

---

## 9. 协议设计

### 9.1 生成请求体

```json
{
  "model": "gpt-draw-1024x1536",
  "stream": false,
  "messages": [
    {
      "role": "user",
      "content": "生成一只可爱的小狗图片"
    }
  ]
}
```

### 9.2 编辑请求体

```json
{
  "model": "gpt-draw-1024x1536",
  "stream": false,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "把人物改成动漫风格"
        },
        {
          "type": "image_url",
          "image_url": {
            "url": "data:image/jpeg;base64,..."
          }
        }
      ]
    }
  ]
}
```

### 9.3 接口请求头

```text
Authorization: Bearer <api_key>
Content-Type: application/json
```

---

## 10. 响应解析设计

### 10.1 响应结构假设

```json
{
  "choices": [
    {
      "message": {
        "content": "![image](data:image/png;base64,...)"
      }
    }
  ]
}
```

### 10.2 解析规则

1. 读取 `choices[0].message.content`
2. 匹配 Markdown 图片语法
3. 提取括号中的 `data:image/...;base64,...`
4. 校验前缀是否为 `data:image/`
5. 解析 MIME 类型与 base64 内容
6. 解码为图片二进制
7. 存入本地缓存目录

### 10.3 失败判定

以下情况视为接口返回不可用：

- `choices` 为空
- `message.content` 为空
- `content` 中没有 Markdown 图片
- 图片 URL 不是 `data:image/...`
- base64 解码失败

---

## 11. 提示词处理设计

- 命令层只负责去除命令前缀、解析数量并保留用户输入的提示词正文。
- 提示词不会调用 AstrBot 已配置模型做英文扩写或翻译。
- `&` 不具备触发能力，会作为普通字符继续传给图片接口。
- 若配置了 `negative_prompt`，插件会在发送图片接口前追加 `Negative prompt: <负面提示词>`。
- 负面提示词同时作用于文本生成、图片编辑与 QQ 头像编辑流程。

---

## 12. 图片提取与编辑输入设计

### 12.1 提取优先级

`/oaiedit` 的图片提取优先级如下：

1. 回复/引用消息中的图片
2. 当前消息附带图片

### 12.2 图片选择策略

- 每次编辑只取第一张有效图片
- 若检测到多张图片，不做多图编辑，首版仅取首张

### 12.3 图片转换

输入图片在发送编辑请求前，必须转换为：

```text
data:image/<mime>;base64,<encoded>
```

需识别常见 MIME：

- `image/png`
- `image/jpeg`
- `image/webp`

---

## 13. 并发设计

### 13.1 并发目标

插件需要支持可配置最大并发数量，避免高频图片任务导致过载。

### 13.2 实现约束

- 插件内部维护统一信号量，例如 `asyncio.Semaphore(max_concurrency)`
- 无论是 `/oaiimg` 还是 `/oaiedit`，都共享同一并发池
- 当单次命令要求生成多张图时：
  - 按数量拆成多个子任务
  - 子任务受同一并发池限制

---

## 14. 缓存设计

### 14.1 缓存目录

缓存目录必须使用 AstrBot 标准插件数据目录：

`data/plugin_data/astrbot_plugin_openai_image/images/`

实现中应通过 `pathlib.Path` 与 AstrBot 路径工具获取，不硬编码字符串拼接。

### 14.2 文件命名

建议命名格式：

```text
YYYYMMDD_HHMMSS_<随机短串>.<扩展名>
```

### 14.3 缓存淘汰

每次成功写入图片后执行缓存清理：

1. 读取缓存目录所有图片文件
2. 按修改时间升序排序
3. 若总数超过 `max_cache_images`
4. 删除最旧文件直到数量回到阈值内

---

## 15. 错误处理设计

### 15.1 用户输入错误

- 提示词为空：返回命令用法
- 数量不是正整数：返回参数错误
- `/oaiedit` 没有图片：提示正确上传方式

### 15.2 接口错误

- HTTP 超时
- 认证失败
- 非 2xx 响应
- 响应 JSON 结构不符合预期
- 图片数据解析失败

### 15.3 平台发送错误

- 当前事件不属于 OneBot v11 平台
- 图片已成功生成，但回传到 QQ 失败

### 15.4 批量部分失败

- 若部分成功、部分失败，先返回成功图片
- 再附带失败数量说明

---

## 16. 日志设计

### 16.1 日志目标

日志需要覆盖以下问题排查：

- 命令参数是否正确解析
- 图片接口是否调用成功
- 响应解析失败发生在哪个阶段
- 并发等待是否导致耗时增加
- QQ 图片发送是否成功

### 16.2 必须记录的耗时信息

- 单次命令总耗时
- 图片接口请求耗时
- 响应解析耗时
- 图片落盘耗时
- 并发排队等待耗时
- OneBot v11 发送耗时

### 16.3 建议日志字段

- `task_id`
- `mode`
- `count`
- `model`
- `elapsed_ms`
- `total_elapsed_ms`

### 16.4 关键日志点

- 命令开始执行
- 进入并发池前后
- 图片接口请求开始与结束
- 响应解析完成
- 图片写入缓存完成
- QQ 图片发送完成
- 全部任务结束

### 16.5 日志示例

```text
[OpenAIImage][generate][task_id=abc123] 开始执行 count=2 model=gpt-draw-1024x1536
[OpenAIImage][queue][task_id=abc123] 并发等待完成 elapsed_ms=134
[OpenAIImage][request][task_id=abc123] 图片接口响应完成 elapsed_ms=5120
[OpenAIImage][parse][task_id=abc123] 响应解析完成 elapsed_ms=17
[OpenAIImage][cache][task_id=abc123] 图片落盘完成 elapsed_ms=9 path=data/plugin_data/astrbot_plugin_openai_image/images/20260422_xxx.png
[OpenAIImage][send][task_id=abc123] OneBot v11 QQ 图片发送完成 elapsed_ms=86
[OpenAIImage][done][task_id=abc123] 任务完成 success=2 failed=0 total_elapsed_ms=6137
```

### 16.6 脱敏要求

- 不打印完整 `api_key`
- 不打印完整 base64 图片数据
- 不打印完整原始响应体
- 提示词若过长，可截断后输出

---

## 17. 模块设计建议

### 17.1 `main.py`

职责：

- 插件注册
- 初始化配置
- 组装服务对象
- 挂载命令处理逻辑

### 17.2 `RequestBuilder`

职责：

- 构建生成请求体
- 构建编辑请求体

### 17.3 `ResponseParser`

职责：

- 解析响应 JSON
- 提取 Markdown 图片
- 解析 `data:image`

### 17.4 `ImageCacheStore`

职责：

- 落盘图片
- 返回文件路径
- 清理旧缓存

### 17.6 `ImageTaskService`

职责：

- 控制并发
- 批量调度
- 汇总任务结果

### 17.7 `ResultPresenter`

职责：

- 将成功图片转换为 AstrBot 图片消息组件
- 通过 OneBot v11 平台将图片发送回 QQ 消息
- 平台发送失败时记录错误信息与耗时

---

## 18. 测试设计

### 18.1 单元测试

- 生成请求体构造正确
- 编辑请求体构造正确
- 负面提示词为空时请求体保持原提示词不变
- 负面提示词非空时追加到文生图和图生图文本提示词中
- `&` 按普通提示词字符保留
- Markdown `data:image` 解析正确
- 非法响应判定正确
- 图片缓存文件命名正确
- 缓存淘汰逻辑正确
- 图片提取优先级正确
- 关键耗时日志字段完整

### 18.2 集成测试

- `/oaiimg` 单张生成成功
- `/oaiimg` 多张生成在并发限制下成功
- `/oaiedit` 同条消息带图成功
- `/oaiedit` 回复图片成功
- 成功结果能通过 OneBot v11 返回到 QQ 消息
- 当前平台不是 OneBot v11 时能给出明确提示
- 平台发送失败时能记录发送阶段耗时与错误原因

---

## 19. 实现约束

- 所有源码文件必须使用 UTF-8 编码
- 新增注释必须使用中文
- 函数名、变量名需清晰规范
- 路径处理统一使用 `pathlib.Path`
- 插件数据目录应通过 AstrBot 路径工具获取
- 代码结构必须体现分层设计

---

## 20. 首版验收标准

满足以下条件即可视为首版完成：

- 能通过 `/oaiimg` 生成图片
- 能通过 `/oaiedit` 编辑图片
- `/oaiedit` 支持“同消息附图”与“回复图片”
- 支持配置 `base_url`、`model`、`api_key`、`negative_prompt`
- 支持配置最大并发数量
- 支持配置最大缓存图片数量
- 响应中的 Markdown `data:image` 能正确解析并发送
- 最终图片结果能通过 OneBot v11 返回到 QQ 消息
- 配置页不再显示“英文扩写模型”配置项
- 缓存目录正确落在 `data/plugin_data/astrbot_plugin_openai_image/images/`
- 关键阶段能输出响应时间日志与总耗时日志

---

## 21. 后续扩展方向

- 多图输入编辑
- 多提供商兜底链路
- 图片结果重发命令
- 用户级并发限制
- WebUI 可视化调试面板
