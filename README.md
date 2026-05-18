# astrbot_plugin_openai_image

基于 OpenAI 兼容图片接口的 AstrBot 图片插件。

当前能力：
- `/oaiimg [数量] [--size 尺寸] <提示词>`
- `/oaiedit [数量] [--size 尺寸] <提示词>`，支持同一来源内多张输入图
- `/oaiqlogo [数量] [--size 尺寸] @用户 <提示词>`
- `/oaifigure`，在同条消息带图或回复图片时设置/更新机器人形象图
- 可在配置中选择 `responses` 或 `images` 端点，`images` 模式支持 `/images/generations` 与 `/images/edits`
- 支持配置全局负面提示词，并自动追加到生成与编辑请求中
- 支持配置默认输出尺寸，也可通过命令参数临时覆盖
- 支持按图片接口供应商单独配置显式代理，网页后台生图和图片编辑同样生效
- 默认仅 Bot 管理员可通过命令参数控制数量、尺寸、质量和审核，可在配置中关闭限制
- 结果缓存到 `data/plugin_data/astrbot_plugin_openai_image/images`
- 可启用内置网页后台，查看历史图库、预览大图，并在网页端执行生图和图片编辑
- 仅支持通过 OneBot v11 回传 QQ 图片消息
- 提供两个函数工具：
  - `openai_generate_image`
  - `openai_edit_image`
  - `openai_edit_robot_figure_image`

## 机器人形象图

使用 `/oaifigure` 设置机器人形象图。形象图会保存到 `data/plugin_data/astrbot_plugin_openai_image/figure/robot_figure.png`，重复设置会覆盖旧图。

当 LLM 判断用户想生成机器人自己、Bot 形象、助手形象、看板娘、机器人头像/立绘/表情包等相关图像时，可自动调用 `openai_edit_robot_figure_image`，插件会使用已保存的形象图作为参考图执行图片编辑。

## 网页后台

在插件配置中开启 `web_admin_enabled`，并设置：

- `web_admin_host`：默认 `127.0.0.1`，仅本机访问；Docker 或局域网访问可改为 `0.0.0.0`
- `web_admin_port`：默认 `7865`
- `web_admin_password`：后台登录密码，留空时后台不会启动

启动后访问 `http://127.0.0.1:7865`。后台提供历史图片小图预览、右侧大图查看、网页文生图和上传图片编辑。编辑模式支持上传或直接从剪贴板粘贴参考图片；预览区支持双击查看原图和复制图片到剪贴板。网页生成与编辑结果会继续保存到同一个图片缓存目录。

## 输出尺寸

可在插件配置中设置 `image_size` 作为默认输出尺寸；留空时不向接口传递尺寸字段，保持上游默认行为。命令参数 `--size` 或 `-s` 会覆盖本次请求的默认尺寸。

示例：

```text
/oaiimg --size portrait 生成一张竖版角色立绘
/oaiimg 2 -s 1536x1024 生成横版风景图
/oaiedit --size square 改成动漫头像
```

支持的别名包括 `auto`、`square`、`portrait`、`landscape`、`2k-square`、`2k-landscape`、`2k-portrait`、`4k-landscape`、`4k-portrait`。也可以使用 `1024x1024` 这类宽高格式，宽高需要是 16 的倍数、单边不超过 3840，长短边比例不超过 3:1。

## 图片接口代理

可在每个图片接口供应商中单独设置 `proxy_url`，例如 `http://127.0.0.1:7890`。该代理只在当前启用供应商请求 OpenAI 兼容图片接口并读取响应时使用，因此命令、LLM 工具和网页后台的文生图/图片编辑都会走该供应商的代理；网页后台页面访问、图库读取、图片回传和提示词优化接口不会受该配置影响。
