# -*- coding: utf-8 -*-
"""把总结文档同步到 OpenViking。宿主机离线时自动跳过，上线后重跑即可。

用法：
    python tools_sync_openviking.py           # 同步
    python tools_sync_openviking.py --check   # 只检查连通性
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.expanduser("~/.openviking/ovcli.conf")
DOC = os.path.join(HERE, "docs", "SUMMARY.md")
TARGET = "viking://resources/pricewatch-hardware-scraper/SUMMARY.md"


def load_conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"读不到 {CONF}: {e}")
        return None


def check(conf, timeout=6):
    """探活。tailnet 域名 + 直连 IP 都试。"""
    urls = [conf.get("url", "").rstrip("/") + "/health"]
    urls.append("http://100.87.238.118:1933/health")
    for u in urls:
        if not u or u == "/health":
            continue
        try:
            req = urllib.request.Request(u, headers={"Authorization": f"Bearer {conf.get('api_key','')}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                print(f"✓ {u} → {r.status}")
                return True
        except Exception as e:
            print(f"✗ {u} → {type(e).__name__}: {str(e)[:80]}")
    return False


def main():
    only_check = "--check" in sys.argv
    conf = load_conf()
    if not conf:
        return 1

    if not os.path.exists(DOC):
        print(f"找不到文档: {DOC}")
        return 1

    print("检查 OpenViking 连通性…")
    if not check(conf, timeout=8):
        print("\n宿主机不可达。请确认 desktop-kkd51rq 已开机且 tailscale 在线，然后重跑本脚本。")
        print("代码与文档已在 GitHub：https://github.com/cxkctrl24-lgtm/pricewatch")
        return 2

    if only_check:
        return 0

    # 连通了就走 MCP 写入（这里只做提示，实际写入用 agent 的 MCP 工具）
    print("\n联通正常。请让 agent 用 mcp__openviking__write 写入：")
    print(f"  uri: {TARGET}")
    print(f"  content: {DOC} 的内容")
    return 0


if __name__ == "__main__":
    sys.exit(main())