# chatgpt-imagegen

[![CI](https://github.com/leeguooooo/chatgpt-imagegen/actions/workflows/ci.yml/badge.svg)](https://github.com/leeguooooo/chatgpt-imagegen/actions/workflows/ci.yml)

[English](./README.md) | **中文**

**用你的 ChatGPT 订阅生图 —— 不需要 `OPENAI_API_KEY`。**

一个零依赖的单文件 Python 命令行工具(也是 AI-agent skill),纯 stdlib。**免费 ChatGPT 账号也能用**——默认后端就是驱动普通的 ChatGPT 网页对话,免费档也能生图。

```bash
chatgpt-imagegen "一只坐在窗台的水彩橘猫" -o cat.png
# -> saved: cat.png  (812,344 bytes)  size=1024x1024  quality=medium
```

<img width="1494" height="870" alt="image" src="https://github.com/user-attachments/assets/b48b0563-58a3-41ff-a207-f01eafbf2ccb" />

---

## 安装

需要 Python 3.10+ 和一个 ChatGPT 订阅(免费档也行)。

**给 AI agent 用(推荐)**——把 skill 装进 Claude Code、Codex、Cursor 等:

```bash
npx skills add leeguooooo/chatgpt-imagegen -g
```

然后直接说:*"画一张 …"*。

**独立命令行**——不用 `pip`、不用虚拟环境:

```bash
git clone https://github.com/leeguooooo/chatgpt-imagegen
sudo install chatgpt-imagegen/chatgpt-imagegen /usr/local/bin/chatgpt-imagegen
```

还需要**一个后端**——`web`(默认,驱动你登录着的 Chrome,不花 Codex 用量)或 `codex`(无头兜底)。`chatgpt-imagegen doctor` 看哪个就绪。→ **[后端与排错](https://drawstyle.leeguoo.com/docs/backends)**

## 用法

```bash
chatgpt-imagegen "阴郁的山间日落" -o web/hero.png --size 1536x1024
chatgpt-imagegen "改成暖调黄昏、电影感 35mm" -i photo.jpg          # 改一张参考图
chatgpt-imagegen "一个机器人吉祥物" --style doodle                  # 套用已存的风格
OUT=$(chatgpt-imagegen "icon" --quiet)                             # 只拿路径(便于管道)
```

完整参数:`chatgpt-imagegen --help`。→ **[生成图片](https://drawstyle.leeguoo.com/docs/generate)** · **[风格系统](https://drawstyle.leeguoo.com/docs/styles)**

## 社区风格

浏览、复用别人调好的画风——公共画廊在 **[drawstyle.leeguoo.com](https://drawstyle.leeguoo.com)**,不用更新脚本:

```bash
chatgpt-imagegen "一只狐狸咖啡师" --style-online pip     # 直接用画廊风格生图,本地不落盘
chatgpt-imagegen style search "水彩 吉祥物"              # 搜索画廊
chatgpt-imagegen style publish mystyle --category cute --from-last   # 分享你的(需一次登录)
```

→ **[用画廊的风格](https://drawstyle.leeguoo.com/docs/community)** · **[投稿与审核](https://drawstyle.leeguoo.com/docs/submit)**

## 了解更多

- 📖 **[完整文档](https://drawstyle.leeguoo.com/docs)** —— 安装、生图、风格、后端、平台。
- 🎨 **[画风画廊](https://drawstyle.leeguoo.com)** —— 浏览与投稿社区画风。
- 📝 **[博客深入](https://blog.leeguoo.com/zh/posts/chatgpt-imagegen/)** —— 背后的设计与原理。
- ⚙️ **[工作原理](./docs/how-it-works.zh-CN.md)** · **[HTTP API 封装](https://github.com/leeguooooo/agent-cli-to-api)**

## 许可

MIT —— 见 [LICENSE](./LICENSE)。

## 免责声明

本工具调用 ChatGPT 内部的 `backend-api/codex` 接口——和官方 Codex CLI 用的是同一个。它不是有文档的公开 API,OpenAI 随时可能更改或限制。请自担风险,并遵守 [OpenAI 使用条款](https://openai.com/policies/row-terms-of-use/)——尤其**不要用你的 ChatGPT 订阅去支撑一个对外公开的生图服务**。

<details>
<summary>关键词</summary>

用 ChatGPT 订阅生成图片、免费 ChatGPT 账号生图、ChatGPT Plus 生图工具、不用 API key 生图、gpt-image-2 用订阅、ChatGPT 订阅生图 CLI、Codex CLI 生图能力独立工具、给 AI agent 用的生图 skill、本地生图脚本、零依赖 Python 生图工具。
</details>
