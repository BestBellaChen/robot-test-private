import os
import asyncio
from playwright.async_api import async_playwright
import requests
import difflib

# ==================== 配置区域 ====================
# 所有敏感信息均通过环境变量注入，不再硬编码在代码中
TARGET_URL = os.environ.get("TARGET_URL", "https://bestbellachen.github.io/robot-test-private/")
FEISHU_WEBHOOKS = os.environ.get("FEISHU_WEBHOOKS", "").split(",") if os.environ.get("FEISHU_WEBHOOKS") else []
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
HISTORY_FILE = "last_record.txt"
# ==================================================

async def fetch_clean_text(url):
    """
    Use Playwright to fetch dynamically rendered page:
    wait for table elements to finish rendering, then extract clean text.
    """
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )

            await page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait for table elements to render (any of table / tbody / tr / class with 'table')
            try:
                await page.wait_for_selector(
                    "table, tbody, tr, [class*='table'], [class*='Table']",
                    state="visible",
                    timeout=15000,
                )
            except Exception:
                print("[提示] 未检测到明确表格元素，继续提取全页文本。")

            # Extra wait to ensure dynamic content is fully rendered
            await page.wait_for_timeout(3000)

            # Extract visible text after removing noise elements
            text = await page.evaluate("""
                () => {
                    document.querySelectorAll(
                        'script, style, noscript, meta, link, svg, button'
                    ).forEach(el => el.remove());
                    return document.body ? document.body.innerText : '';
                }
            """)

            await browser.close()

            lines = [line.strip() for line in text.splitlines() if line.strip()]
            return "\n".join(lines)

    except Exception as e:
        print(f"[错误] 网页抓取失败: {e}")
        return None


def analyze_diff(old_text, new_text):
    """
    对比新旧文本，提取具体的新增和删除内容
    """
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()

    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))

    added_lines = []
    removed_lines = []

    for line in diff:
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added_lines.append(line[1:].strip())
        elif line.startswith("-"):
            removed_lines.append(line[1:].strip())

    return added_lines, removed_lines


def summarize_with_deepseek(added_lines, removed_lines):
    """
    将变更内容发送给 DeepSeek 大模型，让其归纳总结变化要点。
    返回总结文本，失败或未配置 API Key 则返回 None。
    """
    if not DEEPSEEK_API_KEY:
        print("[跳过] 未配置 DEEPSEEK_API_KEY 环境变量，跳过 AI 总结。")
        return None

    # 拼接变更内容供模型分析（最多各取 30 行，避免 token 过长）
    diff_text_parts = []
    if added_lines:
        diff_text_parts.append("=== 新增内容 ===")
        diff_text_parts.extend(added_lines[:30])
    if removed_lines:
        diff_text_parts.append("=== 移除/修改前内容 ===")
        diff_text_parts.extend(removed_lines[:30])

    diff_text = "\n".join(diff_text_parts)

    system_prompt = (
        "你是一个网页内容监控助手。用户会给你网页内容的前后差异（新增和删除的行），"
        "请你用 3~5 句简洁的中文总结：这次更新主要改了什么、新增了什么、删除了什么。"
        "只输出总结本身，不要输出任何多余的开场白或结尾语。"
    )

    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"以下为网页内容变更差异：\n\n{diff_text}"},
                ],
                "temperature": 0.3,
                "max_tokens": 600,
            },
            timeout=30,
        )
        result = resp.json()

        if "choices" in result and len(result["choices"]) > 0:
            summary = result["choices"][0]["message"]["content"].strip()
            return summary
        else:
            print(f"[DeepSeek 错误] API 返回异常: {result}")
            return None

    except Exception as e:
        print(f"[DeepSeek 错误] 请求失败: {e}")
        return None


def send_feishu_notification(webhook_url, target_url, added_lines, removed_lines, summary=None):
    """
    向飞书发送包含「具体差异」的富文本通知
    """
    if not added_lines and not removed_lines:
        return

    headers = {"Content-Type": "application/json"}

    content_blocks = [
        [{"tag": "text", "text": "监控到网页内容发生了更新。\n"}],
    ]

    # AI 总结放在最前面
    if summary:
        content_blocks.append([{"tag": "text", "text": "🤖 AI 总结:\n"}])
        content_blocks.append([{"tag": "text", "text": f"{summary}\n"}])
        content_blocks.append([{"tag": "text", "text": "----------------------------------------\n"}])

    content_blocks.append(
        [
            {"tag": "text", "text": "🔗 目标链接: "},
            {"tag": "a", "text": "点击查看网页", "href": target_url},
            {"tag": "text", "text": "\n----------------------------------------\n"},
        ]
    )

    if added_lines:
        content_blocks.append([{"tag": "text", "text": "🟢 新增内容:\n"}])
        for line in added_lines[:10]:
            content_blocks.append([{"tag": "text", "text": f" + {line}\n"}])
        if len(added_lines) > 10:
            content_blocks.append(
                [{"tag": "text", "text": "   ... (还有更多新增内容未展示)\n"}]
            )

    if removed_lines:
        content_blocks.append([{"tag": "text", "text": "\n🔴 移除/修改前内容:\n"}])
        for line in removed_lines[:10]:
            content_blocks.append([{"tag": "text", "text": f" - {line}\n"}])
        if len(removed_lines) > 10:
            content_blocks.append(
                [{"tag": "text", "text": "   ... (还有更多删除内容未展示)\n"}]
            )

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": "🎯 网页内容变更详情",
                    "content": content_blocks,
                }
            }
        },
    }

    try:
        res = requests.post(webhook_url, json=payload, headers=headers, timeout=10)
        result = res.json()

        if result.get("code") == 0:
            print("[成功] 飞书通知发送成功！请查看飞书群。")
        else:
            print("❌ [飞书拦截] 消息发送失败！")
            print(f"错误码: {result.get('code')}")
            print(f"原因: {result.get('msg')}")
    except Exception as e:
        print(f"[错误] 飞书通知发送失败: {e}")


async def main_async():
    print("🚀 开始执行通用网页监控任务...")

    if not FEISHU_WEBHOOKS:
        print("[终止] 未配置 FEISHU_WEBHOOKS 环境变量。")
        return

    current_content = await fetch_clean_text(TARGET_URL)
    if not current_content:
        print("[终止] 未能获取到有效的网页内容。")
        return

    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(current_content)
        print("[初始化] 首次运行，已保存全站纯文本为基准，本次不报警。")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        last_content = f.read()

    if current_content != last_content:
        added, removed = analyze_diff(last_content, current_content)

        if added or removed:
            print(
                f"[发现变更] 找到 {len(added)} 行新增，{len(removed)} 行移除。"
            )
            print("[AI 总结] 正在调用 DeepSeek 归纳变更要点...")
            summary = summarize_with_deepseek(added, removed)
            if summary:
                print(f"[AI 总结] 归纳结果: {summary}")
            print("[通知] 准备发送飞书通知...")
            for webhook in FEISHU_WEBHOOKS:
                send_feishu_notification(webhook, TARGET_URL, added, removed, summary)
        else:
            print("[忽略] 仅发现排版空格变化，无实质内容增减。")

        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            f.write(current_content)
        print("[更新] 本地历史基准已同步。")
    else:
        print("[保持现状] 网页整体内容未发生变化。")


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
