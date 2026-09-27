# astrbot_plugin_openai_image

基于 OpenAI 兼容图片接口的 AstrBot 图片插件。

当前能力：
- `/oaiimg [数量] [--size 尺寸] <提示词>`
- `/oaiedit [数量] [--size 尺寸] <提示词>`，支持同一来源内多张输入图
- `/oaiqlogo [数量] [--size 尺寸] @用户 <提示词>`
- `/oaifigure`，在同条消息带图或回复图片时设置/更新机器人形象图
- `/oaishow`，展示当前已设置的机器人形象图
- 可在每个图片接口供应商中单独配置 `responses` 或 `images` 端点，`images` 模式支持 `/images/generations` 与 `/images/edits`
- 兼容上游返回 `b64_json` 或 `url` 的图片结果，URL 结果会先下载到本地缓存再发送
- 支持配置默认输出尺寸，也可通过命令参数临时覆盖
- 支持按图片接口供应商单独配置显式代理，内置页面生图和图片编辑同样生效
- 默认仅 Bot 管理员可通过命令参数控制数量、尺寸、质量和审核，可在配置中关闭限制
- 结果缓存到 `data/plugin_data/astrbot_plugin_openai_image/images`
- 提供 WebUI 内置「图片工作台」页面，查看历史图库、预览大图，并在页面内执行生图和图片编辑
- 仅支持通过 OneBot v11 回传 QQ 图片消息
- 提供两个函数工具：
  - `openai_generate_image`
  - `openai_edit_image`
  - `openai_edit_robot_figure_image`

## 机器人形象图

使用 `/oaifigure` 设置机器人形象图，使用 `/oaishow` 查看当前已设置的形象图。形象图会保存到 `data/plugin_data/astrbot_plugin_openai_image/figure/robot_figure.png`，重复设置会覆盖旧图。

当 LLM 判断用户想生成机器人自己、Bot 形象、助手形象、看板娘、机器人头像/立绘/表情包等相关图像时，可自动调用 `openai_edit_robot_figure_image`，插件会使用已保存的形象图作为参考图执行图片编辑。

## 图片工作台（WebUI 内置页面）

插件在 `pages/studio/` 下内置一个页面，随插件一起启用，无需任何开关或密码。在 WebUI 的插件页进入「OpenAI 图片生成」插件详情，点击「图片工作台」即可打开，鉴权沿用 AstrBot Dashboard 登录态。

页面包含四个标签页：

- 历史图库：按文件名或提示词搜索、按格式筛选、按时间或名称排序、分页浏览，点击缩略图可在右侧查看大图和提示词等元数据，并支持删除原图和查看原图
- 生图：在页面内提交文生图任务，可设置数量、尺寸、质量和审核级别，支持调用配置好的文本模型优化提示词
- 编辑：上传或直接粘贴多张参考图，提交图片编辑任务
- 设置：配置提示词优化使用的 OpenAI 兼容 Chat Completions 模型

几点说明：

- 页面运行在 Dashboard 的受限 iframe 中，图片由插件后端以 base64 内联返回，列表使用服务端生成的 webp 缩略图并缓存在 `data/plugin_data/astrbot_plugin_openai_image/webui_thumbnails`
- 页面亮暗主题跟随 WebUI 的主题设置
- 「设置」标签页的提示词优化配置保存在 `data/plugin_data/astrbot_plugin_openai_image/prompt_optimizer_settings.json`；图片供应商、默认尺寸、并发等其余参数仍在插件设置页调整
- 新增或删除 `pages/` 下的页面目录后需要重载插件才会生效

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

可在每个图片接口供应商中单独设置 `model`、`endpoint_type` 和 `proxy_url`，例如 `http://127.0.0.1:7890`。其中 `model` 与 `endpoint_type` 只对当前供应商生效；`proxy_url` 只在当前启用供应商请求 OpenAI 兼容图片接口并读取响应时使用，因此命令、LLM 工具和图片工作台的文生图/图片编辑都会走该供应商的代理；图库读取、图片回传和提示词优化接口不会受该配置影响。
