#!/usr/bin/env python3
"""API Key Validator — 后端 API v2"""

import yaml
import json
import asyncio
import aiohttp
import time
import re
from pathlib import Path
from aiohttp import web
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yml"
LOGS_DIR = Path(__file__).parent / "logs"
MAX_LOG_FILES_PER_PROVIDER = 3


def safe_filename(name):
    """将 provider 名称转为安全的文件名"""
    return re.sub(r'[^\w\u4e00-\u9fff\-]', '_', name).strip('_') or 'unnamed'


def write_provider_log(provider_name, log_entry):
    """将一条日志写入 provider 的日志文件，并清理超出上限的旧文件"""
    LOGS_DIR.mkdir(exist_ok=True)
    today = time.strftime("%Y%m%d")
    safe_name = safe_filename(provider_name)
    log_file = LOGS_DIR / f"{safe_name}_{today}.log"

    # 写入日志（追加模式）
    timestamp = time.strftime("%H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        if isinstance(log_entry, dict):
            detail = log_entry.get("detail", "")
            method = log_entry.get("method", "POST")
            url = log_entry.get("url", "")
            status = log_entry.get("status", "")
            f.write(f"[{timestamp}] {method} {url} [{status}]\n{detail}\n\n")
        else:
            f.write(f"[{timestamp}] {log_entry}\n\n")

    # 清理：每个 provider 最多保留 MAX_LOG_FILES_PER_PROVIDER 个文件
    pattern = f"{safe_name}_*.log"
    files = sorted(LOGS_DIR.glob(pattern))
    if len(files) > MAX_LOG_FILES_PER_PROVIDER:
        for old_file in files[:-MAX_LOG_FILES_PER_PROVIDER]:
            old_file.unlink(missing_ok=True)


def read_provider_logs(provider_name):
    """读取指定 provider 的所有日志文件，合并返回日志条目列表"""
    LOGS_DIR.mkdir(exist_ok=True)
    safe_name = safe_filename(provider_name)
    files = sorted(LOGS_DIR.glob(f"{safe_name}_*.log"))
    entries = []
    # 每条日志以 [HH:MM:SS] 开头，用正则定位每条日志的起始位置
    entry_re = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s+(\w+)\s+(.+?)\s+\[(\w+)\]', re.MULTILINE)
    for f in files:
        try:
            content = f.read_text(encoding="utf-8")
            # 找到所有条目的起始位置
            matches = list(entry_re.finditer(content))
            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
                block = content[start:end].strip()
                if not block:
                    continue
                time_str, method, url, status = m.groups()
                detail = content[m.end():end].strip("\n")
                entries.append({
                    "time": time_str,
                    "method": method,
                    "url": url,
                    "status": status,
                    "detail": detail,
                    "provider": provider_name,
                })
        except Exception:
            pass
    return entries


def get_provider_last_status(provider_name):
    """从日志文件取最近一条验证记录（POST 请求），返回完整验证结果 dict
    
    返回: {status, model, content, elapsed, error, usage} 或 None
    """
    entries = read_provider_logs(provider_name)
    if not entries:
        return None
    # 只看 POST 请求（验证请求），跳过 GET（获取模型列表）
    post_entries = [e for e in entries if (e.get("method", "")).upper() == "POST"]
    if not post_entries:
        return None
    last = post_entries[-1]
    raw_status = str(last.get("status", "")).strip()
    detail = last.get("detail", "")
    
    # 推断语义状态
    if "Timeout" in detail:
        semantic_status = "timeout"
    else:
        http_status_map = {"200": "available", "401": "auth_error", "429": "rate_limited", "400": "not_supported"}
        semantic_status = http_status_map.get(raw_status, "error" if raw_status else "error")
    
    # 从 detail 提取 elapsed
    elapsed = None
    m = re.search(r"Response \(([\d.]+)s\)", detail)
    if m:
        elapsed = float(m.group(1))
    
    # 从 Request JSON 提取 model
    model = ""
    try:
        req_start = detail.find("─── Request")
        if req_start >= 0:
            req_section_end = detail.find("─── Response")
            if req_section_end < 0:
                req_section_end = len(detail)
            req_text = detail[req_start:req_section_end]
            # 找 "model": "xxx" 
            mm = re.search(r'"model"\s*:\s*"([^"]+)"', req_text)
            if mm:
                model = mm.group(1)
    except Exception:
        pass
    
    # 从 detail 提取回复内容（HTTP 200 时的 JSON body 里的 content 字段）
    content = None
    error = None
    usage = None
    if raw_status == "200":
        # 尝试从 Response JSON 中提取 content
        try:
            # 找 Response 部分的 JSON
            resp_start = detail.find("─── Response")
            if resp_start >= 0:
                resp_text = detail[resp_start:]
                # 找 HTTP 行之后的 JSON
                json_start = resp_text.find("{")
                if json_start >= 0:
                    # 提取 JSON 块（可能多行）
                    brace_depth = 0
                    json_end = json_start
                    for i, ch in enumerate(resp_text[json_start:], json_start):
                        if ch == "{":
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                            if brace_depth == 0:
                                json_end = i + 1
                                break
                    resp_json_str = resp_text[json_start:json_end]
                    resp_json = json.loads(resp_json_str)
                    # OpenAI 格式: choices[0].message.content
                    choices = resp_json.get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        content = (msg.get("content") or "")[:80] or None
                    # Anthropic 格式: content[0].text
                    if not content:
                        content_blocks = resp_json.get("content", [])
                        if isinstance(content_blocks, list) and content_blocks:
                            text = content_blocks[0].get("text", "")
                            content = (text or "")[:80] or None
                    # usage
                    u = resp_json.get("usage")
                    if u:
                        usage = u
        except Exception:
            pass
    elif raw_status in ("401", "429", "400"):
        # 从 Response 提取 error message
        try:
            resp_start = detail.find("─── Response")
            if resp_start >= 0:
                resp_text = detail[resp_start:]
                json_start = resp_text.find("{")
                if json_start >= 0:
                    brace_depth = 0
                    json_end = json_start
                    for i, ch in enumerate(resp_text[json_start:], json_start):
                        if ch == "{":
                            brace_depth += 1
                        elif ch == "}":
                            brace_depth -= 1
                            if brace_depth == 0:
                                json_end = i + 1
                                break
                    resp_json = json.loads(resp_text[json_start:json_end])
                    err_obj = resp_json.get("error", {})
                    if isinstance(err_obj, dict):
                        error = (err_obj.get("message") or "")[:100] or None
                    elif isinstance(err_obj, str):
                        error = err_obj[:100]
        except Exception:
            pass
    
    return {
        "status": semantic_status,
        "model": model,
        "content": content,
        "elapsed": elapsed,
        "error": error,
        "usage": usage,
    }


def clear_provider_logs(provider_name):
    """清空指定 provider 的所有日志文件"""
    safe_name = safe_filename(provider_name)
    for f in LOGS_DIR.glob(f"{safe_name}_*.log"):
        f.unlink(missing_ok=True)


def _rename_provider_log_files(old_name, new_name):
    """重命名 provider 的所有日志文件（用于 provider 改名时）
    
    如果新名字对应的日志文件已存在，强制覆盖（日志非关键数据）。
    """
    safe_old = safe_filename(old_name)
    safe_new = safe_filename(new_name)
    LOGS_DIR.mkdir(exist_ok=True)
    renamed = 0
    for f in LOGS_DIR.glob(f"{safe_old}_*.log"):
        suffix = f.name[len(safe_old):]  # e.g. "_20260711.log"
        new_path = LOGS_DIR / f"{safe_new}{suffix}"
        try:
            if new_path.exists():
                new_path.unlink()  # 强制覆盖已存在的日志文件
                logger.info("Overwriting existing log: %s", new_path.name)
            f.rename(new_path)
            renamed += 1
        except Exception as e:
            logger.warning("Failed to rename log %s → %s: %s", f.name, new_path.name, e)
    if renamed:
        logger.info("Renamed %d log file(s): %s → %s", renamed, safe_old, safe_new)


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        providers = cfg.get("providers", [])
        for p in providers:
            p.setdefault("models", [])
            p.setdefault("selected_model", "")
            p.setdefault("source_url", "")
            p.setdefault("api_keys", [])
        cfg["providers"] = providers
        cfg.setdefault("stream", False)
        return cfg
    return {"providers": [], "stream": False}


def save_config(cfg):
    """原子写入配置文件：先写临时文件再 rename，防止写入一半崩溃导致配置丢失"""
    import tempfile, os
    tmp_fd = None
    tmp_path = None
    try:
        content = yaml.dump(cfg, default_flow_style=False, allow_unicode=True, sort_keys=False)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(CONFIG_PATH.parent), suffix=".yml.tmp"
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CONFIG_PATH))
    except Exception as e:
        # 如果原子写入失败，不要删临时文件（便于排查），但确保不残留
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise RuntimeError(f"保存配置失败: {e}") from e


def normalize_base_url(url):
    """自动补全 https:// 前缀，确保以 /v1 结尾（去掉多余斜杠）"""
    if not url:
        return url
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    # 去掉末尾所有斜杠
    url = url.rstrip("/")
    # 如果末尾不是 /v1，自动补上
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def mask_key(key):
    if not key or len(key) < 8:
        return "***"
    return key[:6] + "***" + key[-4:]


def fmt_headers(headers_dict):
    lines = []
    for k, v in headers_dict.items():
        if k.lower() in ("authorization", "x-api-key"):
            if " " in v:
                prefix, key = v.split(" ", 1)
                lines.append(f"  {k}: {prefix} {mask_key(key)}")
            else:
                lines.append(f"  {k}: {mask_key(v)}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def fmt_json(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)


async def fetch_models_openai(session, base_url, api_key, provider_name):
    """OpenAI 协议: GET /v1/models"""
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    req_log = f"─── Request ───\nGET {url}\n{fmt_headers(headers)}"

    try:
        start = time.time()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            elapsed = time.time() - start
            body = await resp.text()
            status = resp.status
            body_json = None
            try:
                body_json = json.loads(body)
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
            except Exception:
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

        log = {"provider": provider_name, "method": "GET", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

        if status == 200:
            if body_json and "data" in body_json:
                models = sorted([m["id"] for m in body_json.get("data", [])])
                return {"ok": True, "models": models, "log": log}
            else:
                return {"ok": False, "error": "响应格式异常：缺少 data 字段", "models": [], "log": log}
        elif status == 401:
            return {"ok": False, "error": "Invalid API Key", "models": [], "log": log}
        else:
            return {"ok": False, "error": f"HTTP {status}", "models": [], "log": log}
    except asyncio.TimeoutError:
        log = {"provider": provider_name, "method": "GET", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout (15s)"}
        return {"ok": False, "error": "请求超时", "models": [], "log": log}
    except Exception as e:
        log = {"provider": provider_name, "method": "GET", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {e}"}
        return {"ok": False, "error": str(e), "models": [], "log": log}


async def validate_openai(session, base_url, api_key, model, provider_name, stream=False, timeout=30):
    """OpenAI 协议: POST /v1/chat/completions"""
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
    if stream:
        payload["stream"] = True

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    try:
        start = time.time()
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            elapsed = time.time() - start
            body = await resp.text()
            status = resp.status

            if stream and status == 200:
                # 流式响应：截取前几行
                lines = body.strip().split("\n")[:5]
                preview = "\n".join(lines)
                if len(body.strip().split("\n")) > 5:
                    preview += f"\n... (共 {len(body.strip().split(chr(10)))} 行)"
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status} (stream)\n{preview}"
                log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}
                # 流式：拼接所有 delta.content
                collected_content = ""
                usage = {}
                for line in body.strip().split("\n"):
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            if chunk.get("usage"):
                                usage = chunk["usage"]
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                c = delta.get("content", "")
                                if c:
                                    collected_content += c
                        except Exception:
                            pass
                return {
                    "ok": True, "status": "available", "model": model,
                    "stream": True, "usage": usage,
                    "content": collected_content[:80] if collected_content else "",
                    "elapsed": elapsed,
                    "log": log,
                }
            else:
                body_json = None
                try:
                    body_json = json.loads(body)
                    resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
                except Exception:
                    resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

                log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

                if status == 200:
                    if not isinstance(body_json, dict):
                        return {"ok": False, "status": "error", "model": model, "error": "响应格式异常", "log": log}
                    usage = body_json.get("usage", {})
                    content = ""
                    choices = body_json.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")[:80]
                    return {
                        "ok": True, "status": "available", "model": model,
                        "stream": False, "usage": usage, "content": content, "elapsed": elapsed,
                        "log": log,
                    }
                elif status == 429:
                    return {"ok": False, "status": "rate_limited", "model": model, "log": log}
                elif status == 401:
                    return {"ok": False, "status": "auth_error", "model": model, "log": log}
                elif status == 400:
                    err = body_json.get("error", {}).get("message", "") if isinstance(body_json, dict) else body[:100]
                    return {"ok": False, "status": "not_supported", "model": model, "error": err[:100], "log": log}
                else:
                    return {"ok": False, "status": "error", "model": model, "error": f"HTTP {status}", "log": log}
    except asyncio.TimeoutError:
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout ({timeout}s)"}
        return {"ok": False, "status": "timeout", "model": model, "log": log}
    except Exception as e:
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {e}"}
        return {"ok": False, "status": "error", "model": model, "error": str(e), "log": log}


async def validate_anthropic(session, base_url, api_key, model, provider_name, timeout=30):
    """Anthropic 协议: POST /v1/messages"""
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    payload = {"model": model, "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    try:
        start = time.time()
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            elapsed = time.time() - start
            body = await resp.text()
            status = resp.status
            body_json = None
            try:
                body_json = json.loads(body)
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
            except Exception:
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

            log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

            if status == 200:
                if not isinstance(body_json, dict):
                    return {"ok": False, "status": "error", "model": model, "error": "响应格式异常", "log": log}
                usage = body_json.get("usage", {})
                content = ""
                content_arr = body_json.get("content", [])
                if content_arr:
                    content = content_arr[0].get("text", "")[:80]
                return {"ok": True, "status": "available", "model": model, "usage": usage, "content": content, "elapsed": elapsed, "log": log}
            elif status == 401:
                return {"ok": False, "status": "auth_error", "model": model, "log": log}
            elif status == 429:
                return {"ok": False, "status": "rate_limited", "model": model, "log": log}
            elif status == 400:
                err = body_json.get("error", {}).get("message", "") if isinstance(body_json, dict) else ""
                return {"ok": False, "status": "not_supported", "model": model, "error": err[:100], "log": log}
            else:
                return {"ok": False, "status": "error", "model": model, "error": f"HTTP {status}", "log": log}
    except asyncio.TimeoutError:
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout ({timeout}s)"}
        return {"ok": False, "status": "timeout", "model": model, "log": log}
    except Exception as e:
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {e}"}
        return {"ok": False, "status": "error", "model": model, "error": str(e), "log": log}


# ─── Web 路由 ──────────────────────────────────────────

async def handle_index(request):
    return web.FileResponse(Path(__file__).parent / "static" / "index.html")


async def handle_get_config(request):
    cfg = load_config()
    providers = cfg.get("providers", [])
    selected_idx = cfg.get("selected_idx", -1)
    # 防越界
    if selected_idx >= len(providers):
        selected_idx = len(providers) - 1 if providers else -1
    # 附带每个 provider 的最近验证状态（优先用 config 中持久化的 last_status）
    for p in providers:
        if not p.get("last_status"):
            p["last_status"] = get_provider_last_status(p.get("name", ""))
    return web.json_response({"providers": providers, "stream": cfg.get("stream", False), "selected_idx": selected_idx})


async def handle_save_config(request):
    body = await request.json()
    new_providers = body.get("providers", [])
    old_cfg = load_config()
    old_providers = old_cfg.get("providers", [])
    old_key_map = {(p.get("name", ""), p.get("base_url", "")): p.get("api_keys", []) for p in old_providers}

    merged = []
    old_status_map = {p.get("base_url", ""): p.get("last_status") for p in old_providers}
    for p in new_providers:
        keys = p.get("api_keys", [])
        if not keys or all("***" in k for k in keys):
            lookup_key = (p.get("name", ""), p.get("base_url", ""))
            keys = old_key_map.get(lookup_key, [])
        # 保留旧 last_status（前端不传此字段）
        ls = p.get("last_status") or old_status_map.get(p.get("base_url", ""))
        merged.append({
            "name": p.get("name", ""),
            "type": p.get("type", "openai"),
            "base_url": p.get("base_url", ""),
            "api_keys": keys,
            "models": p.get("models", []),
            "selected_model": p.get("selected_model", ""),
            "source_url": p.get("source_url", ""),
            "last_status": ls,
        })
    # 检测 provider 改名，重命名对应的日志文件
    old_name_map = {p.get("base_url", ""): p.get("name", "") for p in old_providers}
    for p in merged:
        new_name = p.get("name", "")
        old_name = old_name_map.get(p.get("base_url", ""), "")
        if old_name and old_name != new_name:
            _rename_provider_log_files(old_name, new_name)

    # 检测同名 provider 冲突，自动追加 -1/-2 后缀
    seen_names = {}
    for p in merged:
        name = p.get("name", "")
        if name in seen_names:
            # 冲突，追加后缀
            base = name
            n = 1
            new = f"{base}-{n}"
            while new in seen_names:
                n += 1
                new = f"{base}-{n}"
            p["name"] = new
            seen_names[new] = True
        else:
            seen_names[name] = True

    try:
        save_config({"providers": merged, "stream": body.get("stream", False), "selected_idx": body.get("selected_idx", -1)})
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def handle_fetch_models(request):
    """获取单个 provider 的模型列表并保存到 config
    
    前端可以只传 name（从 config 查找），也可以传完整的 provider 信息（新增未保存时）
    """
    body = await request.json()
    name = body.get("name")
    base_url = body.get("base_url", "")
    api_keys = body.get("api_keys", [])
    ptype = body.get("type", "openai")

    # 优先用前端传来的完整信息，凑不齐再从 config 查
    cfg = load_config()
    providers = cfg.get("providers", [])
    provider = next((p for p in providers if p["name"] == name), None)

    if not provider and not (base_url and api_keys):
        return web.json_response({"error": "provider not found (需要先保存或传入 base_url + api_keys)"}, status=404)

    # 合并：config 里的作为基础，前端传来的字段覆盖
    if provider:
        base_url = base_url or provider.get("base_url", "")
        api_keys = api_keys or provider.get("api_keys", [])
        ptype = ptype if ptype != "openai" or provider.get("type") else provider.get("type", "openai")
    else:
        # 新 provider 不在 config 中，先插入到 config 以便后续保存模型列表
        provider = {"name": name, "type": ptype, "base_url": base_url, "api_keys": api_keys,
                     "models": [], "selected_model": "", "source_url": body.get("source_url", "")}
        providers.append(provider)
        cfg["providers"] = providers

    # key 脱敏还原：如果前端传来带 *** 的 key，从 config 里取真实值
    if api_keys and all("***" in k for k in api_keys) and provider:
        api_keys = provider.get("api_keys", api_keys)

    logs = []
    async with aiohttp.ClientSession() as session:
        if ptype == "openai":
            result = await fetch_models_openai(session, base_url, api_keys[0] if api_keys else "", name)
            logs.append(result["log"])
            write_provider_log(name, result.get("log", {}))
            if result["ok"]:
                if provider:
                    provider["models"] = result["models"]
                    if not provider.get("selected_model") or provider["selected_model"] not in result["models"]:
                        provider["selected_model"] = result["models"][0] if result["models"] else ""
                    save_config(cfg)
                    return web.json_response({"ok": True, "models": result["models"], "selected_model": provider["selected_model"], "logs": logs})
                else:
                    # 新 provider 不在 config 中，返回结果但不保存
                    return web.json_response({"ok": True, "models": result["models"], "selected_model": result["models"][0] if result["models"] else "", "logs": logs})
            else:
                return web.json_response({"ok": False, "error": result["error"], "logs": logs})
        else:
            return web.json_response({"ok": False, "error": "Anthropic 协议不支持模型列表接口"})

    return web.json_response({"ok": False, "error": "未执行"})


async def handle_fetch_all_models(request):
    """批量获取所有 provider 的模型列表"""
    cfg = load_config()
    providers = cfg.get("providers", [])
    all_logs = []
    results = []

    async with aiohttp.ClientSession() as session:
        for p in providers:
            if not p.get("name") or not p.get("base_url"):
                results.append({"name": p.get("name", ""), "ok": False, "error": "缺少配置"})
                continue
            if p["type"] == "openai":
                result = await fetch_models_openai(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", p["name"])
                all_logs.append({**result["log"], "provider": p["name"]})
                write_provider_log(p["name"], result.get("log", {}))
                if result["ok"]:
                    p["models"] = result["models"]
                    if not p.get("selected_model") or p["selected_model"] not in result["models"]:
                        p["selected_model"] = result["models"][0] if result["models"] else ""
                    results.append({"name": p["name"], "ok": True, "models": result["models"], "selected_model": p["selected_model"]})
                else:
                    results.append({"name": p["name"], "ok": False, "error": result["error"]})
            else:
                results.append({"name": p["name"], "ok": False, "error": "Anthropic 协议不支持模型列表"})

    save_config(cfg)
    return web.json_response({"results": results, "logs": all_logs})


async def handle_validate(request):
    """验证单个 provider（用 selected_model 发 completions/messages 请求）
    
    前端可以只传 name（从 config 查找），也可以传完整 provider 信息（新增未保存时）
    """
    body = await request.json()
    name = body.get("name")
    stream = body.get("stream", False)
    base_url = body.get("base_url", "")
    api_keys = body.get("api_keys", [])
    model = body.get("model", "")  # 前端可直接传模型名
    ptype = body.get("type", "openai")

    cfg = load_config()
    providers = cfg.get("providers", [])
    provider = next((p for p in providers if p["name"] == name), None)

    if not provider and not (base_url and api_keys):
        return web.json_response({"error": "provider not found (需要先保存或传入 base_url + api_keys)"}, status=404)

    if provider:
        base_url = base_url or provider.get("base_url", "")
        api_keys = api_keys or provider.get("api_keys", [])
        ptype = ptype or provider.get("type", "openai")
        model = model or provider.get("selected_model", "")
        timeout = provider.get("timeout", 30)
        if api_keys and all("***" in k for k in api_keys):
            api_keys = provider.get("api_keys", api_keys)
    else:
        timeout = 30
        # 不在 config 中且前端没传 model → 报错
        if not model:
            return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    if not model:
        return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    # 支持多 key，并发验证；忽略空值
    keys = [k.strip() for k in api_keys if k.strip()]
    if not keys:
        return web.json_response({"ok": False, "error": "未提供 API Key", "logs": []})

    async def validate_one_key(key_idx, ak):
        """验证单个 key，返回 (result, log)"""
        if ptype == "openai":
            result = await validate_openai(session, base_url, ak, model, name, stream=stream, timeout=timeout)
        elif ptype == "anthropic":
            result = await validate_anthropic(session, base_url, ak, model, name, timeout=timeout)
        else:
            return None, None
        log = result.get("log", {})
        log["key_index"] = key_idx
        log["key_preview"] = ak[:8] + "..." + ak[-4:] if len(ak) > 12 else ak
        result["key_index"] = key_idx
        result["key_preview"] = ak[:8] + "..." + ak[-4:] if len(ak) > 12 else ak
        return result, log

    all_results = []
    logs = []
    timeout_changed = False
    async with aiohttp.ClientSession() as session:
        if ptype not in ("openai", "anthropic"):
            return web.json_response({"ok": False, "error": f"不支持的类型: {ptype}", "logs": []})
        # 并发验证所有 key
        tasks = [validate_one_key(i, ak) for i, ak in enumerate(keys)]
        done = await asyncio.gather(*tasks)
        for result, log in done:
            if result is None:
                continue
            all_results.append(result)
            logs.append(log)
            write_provider_log(name, log)
            if result.get("status") == "timeout" and provider and not timeout_changed:
                new_timeout = min(timeout * 2, 120)
                provider["timeout"] = new_timeout
                timeout_changed = True

    if provider and ("timeout" not in provider or timeout_changed):
        if not timeout_changed:
            provider["timeout"] = 30
        save_config(cfg)

    # 汇总结果：所有 key 都 available → available，否则取最差状态
    statuses = [r.get("status", "error") for r in all_results]
    if all(s == "available" for s in statuses):
        overall_status = "available"
    elif any(s == "auth_error" for s in statuses):
        overall_status = "mixed" if any(s == "available" for s in statuses) else "auth_error"
    else:
        overall_status = statuses[0] if statuses else "error"

    # 返回第一个 result 的字段作为主响应，附加 multi_results
    first = all_results[0]
    # 持久化 last_status 到 config（含 multi_results）
    if provider:
        provider["last_status"] = {
            "status": overall_status,
            "model": first.get("model", model),
            "content": first.get("content"),
            "elapsed": first.get("elapsed"),
            "error": first.get("error"),
            "usage": first.get("usage"),
            "multi_results": all_results if len(keys) > 1 else None,
        }
        save_config(cfg)

    return web.json_response({
        **first,
        "status": overall_status,
        "multi_results": all_results if len(keys) > 1 else None,
        "logs": logs,
    })


async def handle_validate_all(request):
    """批量验证所有 provider（并发，SSE 流式推送）"""
    body = await request.json()
    stream = body.get("stream", False)
    cfg = load_config()
    providers = cfg.get("providers", [])

    async def validate_one(p):
        model = p.get("selected_model", "")
        if not model:
            return {"name": p.get("name", ""), "ok": False, "status": "no_model", "error": "未选择模型"}, None
        timeout = p.get("timeout", 30)
        result = None
        if p["type"] == "openai":
            result = await validate_openai(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", model, p["name"], stream=stream, timeout=timeout)
        elif p["type"] == "anthropic":
            result = await validate_anthropic(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", model, p["name"], timeout=timeout)
        else:
            return {"name": p["name"], "ok": False, "status": "error", "error": f"不支持的类型: {p['type']}"}, None
        log = result.get("log", {})
        r = {**result, "name": p["name"]}
        # 超时翻倍
        if result.get("status") == "timeout":
            new_timeout = min(timeout * 2, 120)
            p["timeout"] = new_timeout
            r["timeout"] = new_timeout
        elif "timeout" not in p:
            p["timeout"] = 30
        return r, log

    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)

    async with aiohttp.ClientSession() as session:
        # 并发验证，每个完成就推送
        tasks = [asyncio.create_task(validate_one(p)) for p in providers]
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                r, log = task.result()
                if log:
                    write_provider_log(r.get("name", ""), log)
                    log_with_name = {**log, "provider": r.get("name", "")}
                    msg = json.dumps({"type": "log", "log": log_with_name}, ensure_ascii=False)
                    await resp.write(f"data: {msg}\n\n".encode())
                msg = json.dumps({"type": "result", "result": r}, ensure_ascii=False)
                await resp.write(f"data: {msg}\n\n".encode())

    save_config(cfg)
    await resp.write(b"event: done\ndata: {}\n\n")
    return resp


async def handle_select_model(request):
    """保存用户选择的模型"""
    body = await request.json()
    name = body.get("name")
    model = body.get("model", "")
    cfg = load_config()
    providers = cfg.get("providers", [])
    provider = next((p for p in providers if p["name"] == name), None)
    if not provider:
        return web.json_response({"error": "provider not found"}, status=404)
    provider["selected_model"] = model
    save_config(cfg)
    return web.json_response({"ok": True, "selected_model": model})


async def handle_delete_provider(request):
    """删除 provider"""
    body = await request.json()
    idx = body.get("idx")
    if idx is None or not isinstance(idx, int):
        return web.json_response({"ok": False, "error": "缺少 idx"}, status=400)
    cfg = load_config()
    providers = cfg.get("providers", [])
    if idx < 0 or idx >= len(providers):
        return web.json_response({"ok": False, "error": f"索引越界: {idx}/{len(providers)}"}, status=400)
    deleted = providers.pop(idx)
    selected_idx = cfg.get("selected_idx", -1)
    if selected_idx >= len(providers):
        selected_idx = len(providers) - 1 if providers else -1
    cfg["selected_idx"] = selected_idx
    save_config(cfg)
    return web.json_response({"ok": True, "deleted": deleted.get("name", ""), "selected_idx": selected_idx})


async def handle_select_provider(request):
    """保存用户选中的 provider 索引"""
    body = await request.json()
    idx = body.get("idx")
    if idx is None or not isinstance(idx, int):
        return web.json_response({"ok": False, "error": "缺少 idx"}, status=400)
    cfg = load_config()
    providers = cfg.get("providers", [])
    if idx < 0 or idx >= len(providers):
        return web.json_response({"ok": False, "error": f"索引越界: {idx}/{len(providers)}"}, status=400)
    cfg["selected_idx"] = idx
    save_config(cfg)
    return web.json_response({"ok": True, "selected_idx": idx})


async def handle_stream(request):
    """保存 stream 设置"""
    body = await request.json()
    stream = body.get("stream", False)
    cfg = load_config()
    cfg["stream"] = stream
    save_config(cfg)
    return web.json_response({"ok": True, "stream": stream})


# 注册字体文件的 MIME 类型
import mimetypes
async def handle_get_logs(request):
    """获取指定 provider 的日志"""
    name = request.query.get("name", "")
    if not name:
        return web.json_response({"error": "缺少 name 参数"}, status=400)
    entries = read_provider_logs(name)
    return web.json_response({"entries": entries})


async def handle_clear_logs(request):
    """清空指定 provider 的日志"""
    body = await request.json()
    name = body.get("name", "")
    if not name:
        return web.json_response({"error": "缺少 name 参数"}, status=400)
    clear_provider_logs(name)
    return web.json_response({"ok": True})


mimetypes.add_type('font/ttf', '.ttf')
mimetypes.add_type('font/woff2', '.woff2')

app = web.Application()
app.router.add_get("/", handle_index)


async def handle_static(request):
    """自定义静态文件 handler，确保字体文件返回正确的 Content-Type"""
    path = request.match_info.get('tail', '')
    full_path = (Path(__file__).parent / "static" / path).resolve()
    # 安全检查：确保路径在 static 目录下
    static_root = (Path(__file__).parent / "static").resolve()
    if not str(full_path).startswith(str(static_root)):
        return web.Response(status=403, text="Forbidden")
    if not full_path.is_file():
        return web.Response(status=404, text="Not Found")
    ct, _ = mimetypes.guess_type(str(full_path))
    return web.FileResponse(full_path, headers={'Content-Type': ct or 'application/octet-stream'})


app.router.add_get("/static/{tail:.*}", handle_static)
app.router.add_get("/api/config", handle_get_config)
app.router.add_post("/api/config", handle_save_config)
app.router.add_post("/api/fetch-models", handle_fetch_models)
app.router.add_post("/api/fetch-all-models", handle_fetch_all_models)
app.router.add_post("/api/validate", handle_validate)
app.router.add_post("/api/validate-all", handle_validate_all)
app.router.add_post("/api/select-model", handle_select_model)
app.router.add_post("/api/select-provider", handle_select_provider)
app.router.add_post("/api/stream", handle_stream)
app.router.add_post("/api/delete-provider", handle_delete_provider)
app.router.add_get("/api/logs", handle_get_logs)
app.router.add_post("/api/clear-logs", handle_clear_logs)

if __name__ == "__main__":
    print("🐱 API Key Validator 启动在 http://0.0.0.0:8899")
    web.run_app(app, host="0.0.0.0", port=8899)
