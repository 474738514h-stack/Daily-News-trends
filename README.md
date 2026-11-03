# 每日风向

日刊式行业热点阅读页（v0.1）：按关注行业与来源筛出当日 Top 10，并提供 AI 秒读 / 深度解读 / 观点碰撞。

## 在线体验

一键打开公开演示（Cloudflare quick tunnel，同域代理 + SiliconFlow API 已接通）：

**[https://tree-packing-fell-planned.trycloudflare.com](https://tree-packing-fell-planned.trycloudflare.com)**

> 演示机重启或 tunnel 重建后 URL 可能变化；以本段链接为准。若仅用 GitHub Pages 托管静态 `docs/morning-feed.html`，页面可打开，但 AI 秒读 / 深度解读 / 观点碰撞需本仓库代理与 SiliconFlow API Key，静态托管 alone 无法调用 `/api/*`。

## 快速开始

```bash
cp .env.example .env.siliconflow   # 填入 SiliconFlow API Key
python3 internal/morning-feed-proxy.py
# 浏览器打开 http://127.0.0.1:8765/morning-feed.html
```

- 离线页：`docs/morning-feed.html`
- 代理：`internal/morning-feed-proxy.py`（同域静态 + `/api/*`）
- 环境变量：仓库根目录 `.env.siliconflow`（模板见 `.env.example`，勿提交密钥）

## 车道与数据

- 车道：AI / 科技 / 军事 / 遥感 / GitHub 开源
- 数据日期：上海 2026-11-03（TrendRadar 热榜 + 五车道补强）

## 每日更新（08:00 Asia/Shanghai）

- 每天 **08:00（Asia/Shanghai）** 换日刊：从当日爬取池里，按用户「关注行业」标签 + 来源筛选，再按最热（🔥 / 交叉来源）与最新时间排序，**固定展示 10 条**。
- 开着的页面会 **软刷新**：轮询 `GET /__mtime`（HTML 被改写时自动 reload），并在下一趟 08:00 定时 soft-reload。
- 代理：`GET /api/schedule`、`GET /api/health` 返回 `showLimit` / `dailyRefresh` / `editionDay` / `nextRefreshAt`。

## 配置弹窗

点「个人偏好」打开弹窗，可设：我所在行业（输入）、关注行业（多选，五车道）、新闻来源（多选）。点「保存」后关闭弹窗并立即按新偏好重排 Top 10。

偏好保存在本机浏览器 `localStorage`（键：`morning-feed-prefs-v2`）。

## 深度解读

桌面列表与手机竖滑卡片均有「深度解读」按钮，可展开/收起。

## 手机

宽度 ≤767px 与桌面同一列表下拉阅读。

## Live LLM（SiliconFlow）

- 页面同域调用 `POST /api/generate`（AI解析 / 深度解读 / AI观点碰撞），**无前端 Mock**。
- 密钥只存在服务端 `.env.siliconflow`，不会出现在公开 HTML。

## AI 跨用户缓存

- 服务端磁盘：`internal/ai-cache/`（JSON；仓库内仅保留空目录）。
- Key：`topicId`（或 `url`）+ `mode`（parse / impact / deep / debate 四轮）+ impact 时的 `industry`。
- 命中直接返回缓存文案（`cached: true`），跳过 SiliconFlow；同 key 并发请求合并为一次生成。
- `debate_followup`（路人甲）不落盘，仍按会话即时生成。
- 每天 **07:50（Asia/Shanghai）** 清空 `ai-cache/`，对齐 **08:00** 日刊冷启动。
- 统计：`GET /api/cache`（含 `dailyWipe` / `nextWipeAt`）。

## 版本

- Tag：`v0.1.0`
