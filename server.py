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

# ─── 单 key 取消机制 ──────────────────────────────────────
# 全局 dict: (name, key_index) -> asyncio.Task
# for validate_and_push 任务，让前端可通过 /api/cancel-key 取消某个 key 的验证
_running_tasks = {}

# 全局 dict: (name, key_index) -> result dict
# 每个 key 验证完后存入，刷新页面后前端可从这里取已完成的中间结果
_completed_results = {}

# 全局 dict: name -> 验证开始时间（unix 时间戳，秒）
# 刷新页面后前端用这个恢复计时
_validation_start_times = {}
# per-key 启动时间，用于刷新恢复时每个 key 独立计时
_key_start_times = {}

# 全局 set: (name, key_index) -> 正在智测的 key（用于刷新恢复时区分验证/智测）
_running_quiz_keys = set()


def _key_preview(ak):
    """统一的 key 脱敏预览：长 key 仅展示前 8 + 后 4，短 key 原样返回"""
    return ak[:8] + "..." + ak[-4:] if len(ak) > 12 else ak



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
                        content = msg.get("content") or None
                    # Anthropic 格式: content[0].text
                    if not content:
                        content_blocks = resp_json.get("content", [])
                        if isinstance(content_blocks, list) and content_blocks:
                            text = content_blocks[0].get("text", "")
                            content = text or None
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
            p.setdefault("timeout", 60)
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


async def fetch_models_openai(session, base_url, api_key, provider_name, extra_headers=None):
    """OpenAI 协议: GET /v1/models"""
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/models"
    # 模拟 codex CLI 的 User-Agent 避免被部分上游 API 通过客户端检测拦截
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "codex_cli_rs/0.18.0"}
    if extra_headers:
        headers.update(extra_headers)
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


async def validate_openai(session, base_url, api_key, model, provider_name, stream=False, timeout=60, prompt_text="hi", extra_headers=None):
    """OpenAI 协议: POST /v1/chat/completions

    prompt_text 默认 "hi"（连通性测试），智力测试时传题目内容。
    题目内容可能较长，max_tokens 自动放大到 500 让模型有足够空间回答。
    """
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/chat/completions"
    # 模拟 codex CLI 的 User-Agent 避免被部分上游 API 通过客户端检测拦截
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.18.0"}
    if extra_headers:
        headers.update(extra_headers)
    # 智力测试题目比 hi 长，智测时不限制 max_tokens 让模型完整回答
    payload = {"model": model, "messages": [{"role": "user", "content": prompt_text}]}
    if prompt_text == "hi":
        payload["max_tokens"] = 50
    if stream:
        payload["stream"] = True

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    start = time.time()
    try:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            status = resp.status
            if stream and status == 200:
                # 真正的流式读取：逐行接收，记录 TTFT 与总耗时
                ttft = None
                collected_content = ""
                usage = {}
                first_lines = []  # 记录前几行用于日志
                async for raw in resp.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if ttft is None:
                        ttft = time.time() - start
                    if len(first_lines) < 5:
                        first_lines.append(line)
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
                elapsed = time.time() - start
                preview = "\n".join(first_lines[:5])
                if len(first_lines) >= 5:
                    preview += f"\n... (TTFT={ttft:.2f}s, 总耗时={elapsed:.2f}s)"
                resp_log = f"─── Response (TTFT={ttft:.2f}s, total={elapsed:.2f}s) ───\nHTTP {status} (stream)\n{preview}"
                log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}
                return {
                    "ok": True, "status": "available", "model": model,
                    "stream": True, "usage": usage,
                    "content": collected_content if collected_content else "",
                    "elapsed": ttft if ttft else elapsed,  # 优先用 TTFT
                    "ttft": ttft,
                    "log": log,
                }
            else:
                body = await resp.text()
                elapsed = time.time() - start
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
                    usage = body_json.get("usage") or body_json.get("data", {}).get("usage", {})
                    content = ""
                    choices = body_json.get("choices") or body_json.get("data", {}).get("choices", [])
                    if choices:
                        msg = choices[0].get("message", {})
                        content = msg.get("content", "")
                        # 部分网关(如 Cline) content 为空时回复在 reasoning 字段
                        if not content:
                            content = msg.get("reasoning", "") or msg.get("reasoning_content", "")
                        content = content if content else ""
                    return {
                        "ok": True, "status": "available", "model": model,
                        "stream": False, "usage": usage, "content": content, "elapsed": elapsed,
                        "log": log,
                    }
                elif status == 429:
                    return {"ok": False, "status": "rate_limited", "model": model, "elapsed": elapsed, "log": log}
                elif status == 401:
                    return {"ok": False, "status": "auth_error", "model": model, "elapsed": elapsed, "error": "HTTP 401 Unauthorized", "log": log}
                elif status == 400:
                    err = body_json.get("error", {}).get("message", "") if isinstance(body_json, dict) else body[:100]
                    return {"ok": False, "status": "not_supported", "model": model, "error": err[:100], "elapsed": elapsed, "log": log}
                else:
                    return {"ok": False, "status": "error", "model": model, "error": f"HTTP {status}", "elapsed": elapsed, "log": log}
    except asyncio.TimeoutError:
        elapsed = time.time() - start
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout ({timeout}s, 耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "timeout", "model": model, "elapsed": elapsed, "log": log}
    except asyncio.CancelledError:
        elapsed = time.time() - start
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏹ Cancelled (耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "cancelled", "model": model, "elapsed": elapsed, "log": log}
    except Exception as e:
        elapsed = time.time() - start
        err_msg = str(e) or type(e).__name__
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {err_msg} (耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "error", "model": model, "error": err_msg, "elapsed": elapsed, "log": log}


async def validate_anthropic(session, base_url, api_key, model, provider_name, stream=False, timeout=60, prompt_text="hi", extra_headers=None):
    """Anthropic 协议: POST /v1/messages

    prompt_text 默认 "hi"（连通性测试），智力测试时传题目内容。
    """
    base_url = normalize_base_url(base_url)
    url = base_url.rstrip("/") + "/messages"
    # 同 OpenAI 协议加 codex CLI UA（保持一致；有些网关对 UA 检测）
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.18.0"}
    if extra_headers:
        headers.update(extra_headers)
    # 智测时给足 max_tokens 让模型完整回答（Anthropic 协议必须传 max_tokens）
    max_tokens = 4096 if prompt_text != "hi" else 50
    payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt_text}]}
    if stream:
        payload["stream"] = True

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    start = time.time()
    try:
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            status = resp.status
            if stream and status == 200:
                # 真正的流式读取：逐行接收 SSE，记录 TTFT
                ttft = None
                collected_text = ""
                usage = {}
                first_lines = []
                # Anthropic SSE 事件结构:
                #   event: message_start / content_block_delta / message_delta / message_stop
                #   data: {...}
                # content_block_delta.data.delta.text = "text..."
                # message_delta.data.usage = {...}
                async for raw in resp.content:
                    line = raw.decode("utf-8", "ignore").strip()
                    if ttft is None and line:
                        ttft = time.time() - start
                    if len(first_lines) < 5:
                        first_lines.append(line)
                    if line.startswith("data: "):
                        try:
                            chunk = json.loads(line[6:])
                            ev_type = chunk.get("type", "")
                            if ev_type == "content_block_delta":
                                delta = chunk.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    collected_text += delta.get("text", "")
                            elif ev_type == "message_delta":
                                if chunk.get("usage"):
                                    usage = chunk["usage"]
                        except Exception:
                            pass
                elapsed = time.time() - start
                preview = "\n".join(first_lines[:5])
                if len(first_lines) >= 5:
                    preview += f"\n... (TTFT={ttft:.2f}s, 总耗时={elapsed:.2f}s)"
                resp_log = f"─── Response (TTFT={ttft:.2f}s, total={elapsed:.2f}s) ───\nHTTP {status} (stream)\n{preview}"
                log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}
                return {
                    "ok": True, "status": "available", "model": model,
                    "stream": True, "usage": usage,
                    "content": collected_text if collected_text else "",
                    "elapsed": ttft if ttft else elapsed,
                    "ttft": ttft,
                    "log": log,
                }
            else:
                body = await resp.text()
                elapsed = time.time() - start
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
                        content = content_arr[0].get("text", "")
                    return {"ok": True, "status": "available", "model": model, "usage": usage, "content": content, "elapsed": elapsed, "log": log}
                elif status == 401:
                    return {"ok": False, "status": "auth_error", "model": model, "elapsed": elapsed, "error": "HTTP 401 Unauthorized", "log": log}
                elif status == 429:
                    return {"ok": False, "status": "rate_limited", "model": model, "elapsed": elapsed, "log": log}
                elif status == 400:
                    err = body_json.get("error", {}).get("message", "") if isinstance(body_json, dict) else ""
                    return {"ok": False, "status": "not_supported", "model": model, "error": err[:100], "elapsed": elapsed, "log": log}
                else:
                    return {"ok": False, "status": "error", "model": model, "error": f"HTTP {status}", "elapsed": elapsed, "log": log}
    except asyncio.TimeoutError:
        elapsed = time.time() - start
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout ({timeout}s, 耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "timeout", "model": model, "elapsed": elapsed, "log": log}
    except asyncio.CancelledError:
        elapsed = time.time() - start
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏹ Cancelled (耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "cancelled", "model": model, "elapsed": elapsed, "log": log}
    except Exception as e:
        elapsed = time.time() - start
        err_msg = str(e) or type(e).__name__
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {err_msg} (耗时 {elapsed:.2f}s)"}
        return {"ok": False, "status": "error", "model": model, "error": err_msg, "elapsed": elapsed, "log": log}


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
    # 从全局 _running_tasks 提取正在验证的 provider 和 key_index 列表
    running = {}
    for (pname, kidx) in _running_tasks:
        task = _running_tasks[(pname, kidx)]
        if not task.done():
            if pname not in running:
                running[pname] = {
                    "key_indices": [],
                    "quiz_key_indices": [],
                    "started_at": _validation_start_times.get(pname, time.time()),
                    "key_started_at": {},
                }
            running[pname]["key_indices"].append(kidx)
            running[pname]["key_started_at"][str(kidx)] = _key_start_times.get((pname, kidx), _validation_start_times.get(pname, time.time()))
            if (pname, kidx) in _running_quiz_keys:
                running[pname]["quiz_key_indices"].append(kidx)
    return web.json_response({"providers": providers, "stream": cfg.get("stream", False), "selected_idx": selected_idx, "running_validations": running})


async def handle_save_config(request):
    body = await request.json()
    new_providers = body.get("providers", [])
    old_cfg = load_config()
    old_providers = old_cfg.get("providers", [])
    old_key_map = {(p.get("name", ""), p.get("base_url", "")): p.get("api_keys", []) for p in old_providers}

    merged = []
    old_status_map = {p.get("name", ""): p.get("last_status") for p in old_providers}
    for p in new_providers:
        keys = p.get("api_keys", [])
        if not keys or all("***" in k for k in keys):
            lookup_key = (p.get("name", ""), p.get("base_url", ""))
            keys = old_key_map.get(lookup_key, [])
        # 保留旧 last_status（按 provider name 取，避免同 base_url 互覆盖）
        ls = p.get("last_status") or old_status_map.get(p.get("name", ""))
        merged.append({
            "name": p.get("name", ""),
            "type": p.get("type", "openai"),
            "base_url": p.get("base_url", ""),
            "api_keys": keys,
            "models": p.get("models", []),
            "selected_model": p.get("selected_model", ""),
            "source_url": p.get("source_url", ""),
            "timeout": p.get("timeout", 60),
            "extra_headers": p.get("extra_headers", {}),
            "last_status": ls,
        })
    # 检测 provider 改名，重命名对应的日志文件
    # 按 (base_url, api_keys 首个) 做指纹匹配，找到旧 name → 新 name 的映射
    new_names = {p.get("name", "") for p in merged}
    for old_p in old_providers:
        old_name = old_p.get("name", "")
        if old_name in new_names:
            continue  # 名字没变
        # 找 base_url 相同的 new provider，认为是改名
        old_url = old_p.get("base_url", "")
        matched = next((p for p in merged if p.get("base_url", "") == old_url and p.get("name", "") not in {op.get("name", "") for op in old_providers}), None)
        if matched:
            _rename_provider_log_files(old_name, matched["name"])

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
        # extra_headers: 前端 body 优先
        eh = body.get("extra_headers")
        provider["extra_headers"] = eh if eh is not None else provider.get("extra_headers", {})
    else:
        # 新 provider 不在 config 中，先插入到 config 以便后续保存模型列表
        provider = {"name": name, "type": ptype, "base_url": base_url, "api_keys": api_keys,
                     "models": [], "selected_model": "", "source_url": body.get("source_url", ""), "extra_headers": body.get("extra_headers", {})}
        providers.append(provider)
        cfg["providers"] = providers

    # key 脱敏还原：如果前端传来带 *** 的 key，从 config 里取真实值
    if api_keys and all("***" in k for k in api_keys) and provider:
        api_keys = provider.get("api_keys", api_keys)

    logs = []
    async with aiohttp.ClientSession() as session:
        if ptype == "openai":
            result = await fetch_models_openai(session, base_url, api_keys[0] if api_keys else "", name, extra_headers=provider.get("extra_headers"))
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
                result = await fetch_models_openai(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", p["name"], extra_headers=p.get("extra_headers"))
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
    prompt_text = body.get("prompt", "hi")  # 默认 hi；智力测试时传题目内容

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
        timeout = body.get("timeout") or provider.get("timeout", 60)
        # extra_headers: 前端 body 优先（未保存的修改也能即时生效），fallback 到 config
        eh = body.get("extra_headers")
        provider["extra_headers"] = eh if eh is not None else provider.get("extra_headers", {})
        if api_keys and all("***" in k for k in api_keys):
            api_keys = provider.get("api_keys", api_keys)
    else:
        timeout = body.get("timeout", 60)
        # 未保存的新 provider：从请求体取 extra_headers
        provider = {"extra_headers": body.get("extra_headers", {})}
        # 不在 config 中且前端没传 model → 报错
        if not model:
            return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    if not model:
        return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    # 支持多 key，并发验证；忽略空值
    keys = [k.strip() for k in api_keys if k.strip()]
    if not keys:
        return web.json_response({"ok": False, "error": "未提供 API Key", "logs": []})

    # 支持单个 key 重测：传入 key_index 时只验证那一个 key
    key_index_filter = body.get("key_index")
    if key_index_filter is not None and 0 <= key_index_filter < len(keys):
        orig_key_index = key_index_filter
        keys = [keys[key_index_filter]]
    else:
        orig_key_index = None

    # 用 SSE 流式响应：每个 key 验证完立刻 push 一条事件，前端实时显示
    resp = web.StreamResponse(status=200, headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
    })
    await resp.prepare(request)

    # 立刻推送 start 事件：列出所有待测 key 的脱敏预览，前端收到即可渲染骨架表格
    # key_index 用原始索引（单 key 重测时也保持对应行号）
    start_payload = {
        "name": name,
        "keys": [{"key_index": (orig_key_index if orig_key_index is not None else i), "key_preview": _key_preview(ak)} for i, ak in enumerate(keys)],
    }
    await resp.write(f"event: start\ndata: {json.dumps(start_payload, ensure_ascii=False)}\n\n".encode("utf-8"))

    async def validate_and_push(key_idx, ak):
        """验证单个 key，完成后立即通过 SSE 推送结果

        被外部 cancel() 取消时，捕获 CancelledError，推送一条 cancelled 状态的
        key_done 事件，让前端把对应行更新为「已停止」。同时把 cancelled 结果登记
        到 cancelled_results，供 done 事件的 multi_results 引用。
        """
        try:
            eh = (provider or {}).get("extra_headers")
            if ptype == "openai":
                result = await validate_openai(session, base_url, ak, model, name, stream=stream, timeout=timeout, prompt_text=prompt_text, extra_headers=eh)
            elif ptype == "anthropic":
                result = await validate_anthropic(session, base_url, ak, model, name, stream=stream, timeout=timeout, prompt_text=prompt_text, extra_headers=eh)
            else:
                return None, None, None
            log = result.get("log", {})
            log["key_index"] = key_idx
            log["key_preview"] = _key_preview(ak)
            result["key_index"] = key_idx
            result["key_preview"] = _key_preview(ak)
            write_provider_log(name, log)
            # 存入全局中间结果表，刷新页面后前端可取
            _completed_results[(name, key_idx)] = result
            # 立刻推给前端（SSE 连接可能已断开，推送失败不影响结果）
            try:
                await resp.write(f"event: key_done\ndata: {json.dumps(result, ensure_ascii=False)}\n\n".encode("utf-8"))
            except Exception:
                pass
            return result, log, None
        except asyncio.CancelledError:
            # 被前端通过 /api/cancel-key 主动取消：推送 cancelled 状态
            cancelled = {
                "ok": False, "status": "cancelled", "model": model,
                "key_index": key_idx, "key_preview": _key_preview(ak),
                "elapsed": None, "error": None,
            }
            try:
                await resp.write(f"event: key_done\ndata: {json.dumps(cancelled, ensure_ascii=False)}\n\n".encode("utf-8"))
            except Exception:
                pass
            _completed_results[(name, key_idx)] = cancelled
            # 不重新抛出：把 cancelled 结果作为正常返回值，让 gather 正常收集
            return None, None, cancelled
        except Exception as e:
            # 判断是 SSE 写入失败还是真正的验证异常
            err_str = str(e)
            is_sse_error = any(s in err_str for s in ("Cannot write to closing transport", "Connection reset", "ClientConnectionError"))
            if is_sse_error:
                # SSE 推送失败，验证本身可能已成功，不覆盖已有的正确结果
                existing = _completed_results.get((name, key_idx))
                if existing:
                    return existing, None, None
                return {"ok": False, "status": "error", "model": model, "error": "SSE connection lost",
                        "key_index": key_idx, "key_preview": _key_preview(ak)}, None, None
            err_result = {"ok": False, "status": "error", "model": model, "error": err_str,
                          "key_index": key_idx, "key_preview": _key_preview(ak)}
            _completed_results[(name, key_idx)] = err_result
            try:
                await resp.write(f"event: key_done\ndata: {json.dumps(err_result, ensure_ascii=False)}\n\n".encode("utf-8"))
            except Exception:
                pass
            return err_result, None, None

    all_results = []   # 完成的验证结果（含 cancelled）按 key_index 顺序排列
    logs = []
    async with aiohttp.ClientSession() as session:
        if ptype not in ("openai", "anthropic"):
            await resp.write(f"event: error\ndata: {json.dumps({'error': f'不支持的类型: {ptype}'}, ensure_ascii=False)}\n\n".encode("utf-8"))
            await resp.write_eof()
            return resp
        # 把每个 key 的验证封装成 asyncio.Task，登记到全局 _running_tasks，
        # 让 /api/cancel-key 可以按 (name, key_index) 取消单个任务
        _validation_start_times[name] = time.time()
        tasks = []
        for i, ak in enumerate(keys):
            # 单 key 重测时用原始 key_index 做映射
            real_idx = orig_key_index if orig_key_index is not None else i
            _key_start_times[(name, real_idx)] = time.time()
            task = asyncio.create_task(validate_and_push(real_idx, ak))
            _running_tasks[(name, real_idx)] = task
            if prompt_text != "hi":
                _running_quiz_keys.add((name, real_idx))
            tasks.append(task)
        # return_exceptions=True：即使某个 task 内部异常不被自身 try/except 兜住，
        # 也不会让整个 gather 抛错；当前实现里 validate_and_push 已吃掉所有异常，
        # 正常路径只会返回 (result, log, cancelled)
        done = await asyncio.gather(*tasks, return_exceptions=True)
        # 清理全局 task 表
        for i, _task in enumerate(tasks):
            real_idx = orig_key_index if orig_key_index is not None else i
            _running_tasks.pop((name, real_idx), None)
            _running_quiz_keys.discard((name, real_idx))
        # 清理中间结果表（全部完成后不再需要）
        for i in range(len(keys)):
            real_idx = orig_key_index if orig_key_index is not None else i
            _completed_results.pop((name, real_idx), None)
        # 清理验证开始时间
        _validation_start_times.pop(name, None)
        for i in range(len(keys)):
            real_idx = orig_key_index if orig_key_index is not None else i
            _key_start_times.pop((name, real_idx), None)
        # 按原 key_index 顺序汇总结果
        for item in done:
            if isinstance(item, BaseException):
                # 防御性：不应走到这里
                continue
            result, log, cancelled = item
            if cancelled is not None:
                all_results.append(cancelled)
                continue
            if result is None:
                continue
            all_results.append(result)
            if log:
                logs.append(log)
        # 按 key_index 排序，保证最终 multi_results 顺序与 key 顺序一致
        all_results.sort(key=lambda r: r.get("key_index", 0))

    # 持久化 last_status（timeout 已由前端设置，不再自动翻倍）
    # 汇总结果：所有 key 都 available → available，否则取最差状态
    statuses = [r.get("status", "error") for r in all_results]
    if not statuses:
        overall_status = "error"
    elif all(s == "available" for s in statuses):
        overall_status = "available"
    elif any(s == "cancelled" for s in statuses) and not any(s == "available" for s in statuses) and not any(s in ("auth_error", "rate_limited", "timeout", "error") for s in statuses):
        # 只有 cancelled 一种状态 → cancelled
        overall_status = "cancelled"
    elif any(s == "auth_error" for s in statuses):
        overall_status = "mixed" if any(s == "available" for s in statuses) else "auth_error"
    else:
        overall_status = statuses[0] if statuses else "error"

    # 持久化 last_status 到 config（含 multi_results）
    # cancelled 也算一个有效 result 入 multi_results，与前端表格结构一致
    if provider and all_results:
        # 优先取第一个非 cancelled 结果作为展示主体
        first = next((r for r in all_results if r.get("status") != "cancelled"), all_results[0])
        merged_multi = all_results  # 默认用本次验证结果
        # 单 key 重测：合并到现有 multi_results 中（替换对应 key_index 位置）
        if orig_key_index is not None and provider.get("last_status", {}).get("multi_results"):
            existing = list(provider["last_status"]["multi_results"])
            # 确保列表足够长
            while len(existing) <= orig_key_index:
                existing.append({"status": "error", "error": "无需试"})
            # 用新结果替换（保留旧的成功 result 如果新结果没返回 content）
            new_result = all_results[0]
            existing[orig_key_index] = new_result
            merged_multi = existing
            # 重新计算 overall status
            merged_statuses = [r.get("status", "error") for r in merged_multi]
            if all(s == "available" for s in merged_statuses):
                overall_status = "available"
            elif any(s == "available" for s in merged_statuses):
                overall_status = "mixed"
            else:
                overall_status = merged_statuses[0]
            # 取第一个 available 的结果作为展示主体
            first = next((r for r in merged_multi if r.get("status") == "available"), first)
        else:
            merged_multi = all_results
        provider["last_status"] = {
            "status": overall_status,
            "model": first.get("model", model),
            "content": first.get("content"),
            "elapsed": first.get("elapsed"),
            "error": first.get("error"),
            "usage": first.get("usage"),
            "multi_results": merged_multi,
        }
        save_config(cfg)

    # 推送最终汇总事件
    summary = {
        "status": overall_status,
        "multi_results": merged_multi if orig_key_index is not None else all_results,
        "logs": logs,
        "total": len(merged_multi) if orig_key_index is not None else len(all_results),
    }
    await resp.write(f"event: done\ndata: {json.dumps(summary, ensure_ascii=False)}\n\n".encode("utf-8"))
    await resp.write_eof()
    return resp


async def handle_cancel_key(request):
    """取消某个 provider 验证流程中指定 key_index 的验证 task

    body: {"name": "...", "key_index": N}
    成功取消（task 存在且未完成）返回 {"ok": true}，否则返回 404 / 200 不存在
    """
    body = await request.json()
    name = body.get("name")
    try:
        key_index = int(body.get("key_index"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "key_index 必须是整数"}, status=400)
    if not name:
        return web.json_response({"ok": False, "error": "缺少 name"}, status=400)
    task = _running_tasks.get((name, key_index))
    if task is None or task.done():
        return web.json_response({"ok": False, "error": "无对应运行中的任务"}, status=404)
    task.cancel()
    return web.json_response({"ok": True, "name": name, "key_index": key_index})


async def handle_validate_status(request):
    """GET /api/validate-status?name=xxx
    返回该 provider 当前验证的中间状态：
    - running: 还在跑的 key_index 列表
    - completed: 已完成的 key 结果列表（从 _completed_results 取）
    - 如果不在验证中：返回 finished=True
    """
    name = request.query.get("name", "")
    if not name:
        return web.json_response({"ok": False, "error": "缺少 name"}, status=400)
    running = []
    completed = []
    for (pname, kidx), task in _running_tasks.items():
        if pname != name:
            continue
        if task.done():
            # 已完成，从 _completed_results 取结果
            r = _completed_results.get((pname, kidx))
            if r:
                completed.append(r)
        else:
            running.append(kidx)
    # 也收集不在 _running_tasks 但在 _completed_results 里的（边界情况）
    for (pname, kidx), r in _completed_results.items():
        if pname == name and r not in completed:
            completed.append(r)
    if not running and not completed:
        return web.json_response({"ok": True, "finished": True})
    completed.sort(key=lambda r: r.get("key_index", 0))
    return web.json_response({"ok": True, "finished": False, "running": running, "completed": completed})


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
        timeout = p.get("timeout", 60)
        result = None
        eh = p.get("extra_headers")
        if p["type"] == "openai":
            result = await validate_openai(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", model, p["name"], stream=stream, timeout=timeout, extra_headers=eh)
        elif p["type"] == "anthropic":
            result = await validate_anthropic(session, p["base_url"], p["api_keys"][0] if p.get("api_keys") else "", model, p["name"], stream=stream, timeout=timeout, extra_headers=eh)
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
            p["timeout"] = 60
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


async def handle_judge_batch(request):
    """用裁判模型批量判定多个 key 的答案对错（一次 LLM 调用搞定 N 个 key）

    body: {
        "question": "...",
        "standard_answer": "...",
        "answers": [{"key_index": 0, "key_preview": "nvapi-...xxx", "answer": "..."}, ...],
        "judge": { name, base_url, api_keys, type, model, timeout }
    }

    返回:
    {
        "results": [{"key_index": 0, "correct": true/false, "reason": "..."}, ...],
        "judge_raw": "裁判原始回复全文（便于调试）",
        "elapsed": 裁判请求耗时
    }

    裁判失败的行不放入 results，前端会把它标「裁判未列出此 key」
    """
    body = await request.json()
    question = body.get("question", "")
    standard_answer = body.get("standard_answer", "")
    answers = body.get("answers", [])
    judge = body.get("judge", {})
    logger.info(f"[judge-batch] judge={judge.get('name')}/{judge.get('model')} answers={len(answers)} keys={[a.get('key_index') for a in answers]}")

    if not judge or not judge.get("base_url") or not judge.get("api_keys"):
        return web.json_response({"results": [], "reason": "裁判 provider 未配置"}, status=400)
    if not answers:
        return web.json_response({"results": [], "reason": "无应答可判定"})

    base_url = judge["base_url"]
    api_key = judge["api_keys"][0] if judge.get("api_keys") else ""
    model = judge.get("model", "")
    jtype = judge.get("type", "openai")
    jtimeout = judge.get("timeout", 60)

    # 构造裁判 prompt：把 N 条答案编号列出，让裁判一次返回 JSON 数组
    answers_block = "\n\n".join(
        f"---\nKey #{a['key_index']} ({a.get('key_preview', '')}):\n{a.get('answer', '')}"
        for a in answers
    )
    # 列出所有 key_index 让裁判知道要判哪些
    keys_list = ", ".join(f"#{a['key_index']}" for a in answers)
    judge_prompt = f"""请判断以下每个 key 对同一道题的回答是否正确。

题目：{question}

标准答案：{standard_answer}

各 key 的回答：
{answers_block}

判断规则：
1. 语义等价即判对（如 "45" vs "40元现金加一包5元零食" vs "小明赚45店员亏45" 都算对）
2. 数字错误判错
3. 答非所问或空回复判错
4. 方向一致且金额正确判对

请只回复一个 JSON 数组，包含所有 key_index ({keys_list}) 的判定：
[
  {{"key_index": 0, "correct": true, "reason": "答对"}},
  {{"key_index": 1, "correct": false, "reason": "数字错误，把50元当5元"}}
]"""

    timeout = aiohttp.ClientTimeout(total=jtimeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            judge_extra = body.get("judge_extra_headers")
            if jtype == "openai":
                result = await validate_openai(session, base_url, api_key, model, "judge-batch", stream=False, timeout=jtimeout, prompt_text=judge_prompt, extra_headers=judge_extra)
            elif jtype == "anthropic":
                result = await validate_anthropic(session, base_url, api_key, model, "judge-batch", stream=False, timeout=jtimeout, prompt_text=judge_prompt, extra_headers=judge_extra)
            else:
                return web.json_response({"results": [], "reason": f"不支持协议: {jtype}"}, status=400)

            content = result.get("content", "")
            logger.info(f"[judge-batch] judge reply len={len(content)} repr={content[:500]!r}")
            if not content:
                return web.json_response({"results": [], "reason": "裁判模型无回复", "elapsed": result.get("elapsed")})

            # 解析裁判回复里的 JSON 数组
            import re
            # 先去掉 markdown 代码块包裹（```json ... ```），裁判可能会加这个
            md_match = re.search(r'```(?:json)?\s*(.*?)\s*```', content, re.DOTALL)
            content_stripped = md_match.group(1) if md_match else content
            # 优先找最外层的 [ ... ]（裁判可能输出额外解释文本）
            # 用宽松匹配：从第一个 [ 到最后一个 ]
            first = content_stripped.find('[')
            last = content_stripped.rfind(']')
            if first >= 0 and last > first:
                json_str = content_stripped[first:last+1]
                try:
                    parsed = json.loads(json_str)
                    results = []
                    for item in parsed:
                        if isinstance(item, dict) and "key_index" in item:
                            results.append({
                                "key_index": item["key_index"],
                                "correct": bool(item.get("correct", False)),
                                "reason": str(item.get("reason", ""))[:200]  # 截断原因长度
                            })
                    if results:
                        logger.info(f"[judge-batch] parsed {len(results)} results: {results}")
                        return web.json_response({
                            "results": results,
                            "judge_raw": content[:2000],
                            "elapsed": result.get("elapsed")
                        })
                except json.JSONDecodeError:
                    pass

            # 启发式 fallback：尝试单条提取 correct 键
            single_match = re.search(r'\{[^}]*"correct"[^}]*\}', content_stripped, re.DOTALL)
            if single_match:
                try:
                    parsed = json.loads(single_match.group())
                    # 如果只有 1 条答案，单条的 correct 适用于它
                    if len(answers) == 1:
                        return web.json_response({
                            "results": [{
                                "key_index": answers[0]["key_index"],
                                "correct": bool(parsed.get("correct", False)),
                                "reason": str(parsed.get("reason", ""))[:200]
                            }],
                            "judge_raw": content[:2000],
                            "elapsed": result.get("elapsed")
                        })
                except json.JSONDecodeError:
                    pass

            logger.warning(f"[judge-batch] failed to parse JSON: {content[:300]}")
            return web.json_response({
                "results": [],
                "reason": f"裁判回复无法解析 JSON 数组: {content[:300]}",
                "judge_raw": content[:2000],
                "elapsed": result.get("elapsed")
            })

    except asyncio.TimeoutError:
        return web.json_response({"results": [], "reason": "裁判超时"}, status=504)
    except Exception as e:
        return web.json_response({"results": [], "reason": f"裁判调用失败: {e}"}, status=500)


async def handle_judge(request):
    """用裁判模型判定答案对错

    body: {
        "question": "...题目原文...",
        "standard_answer": "...标准答案...",
        "model_answer": "...被测模型的回答...",
        "judge": {
            "name": "...", "base_url": "...", "api_keys": ["sk-..."],
            "type": "openai", "model": "...", "timeout": 30
        }
    }

    返回 {"correct": true/false, "reason": "..."}
    """
    body = await request.json()
    question = body.get("question", "")
    standard_answer = body.get("standard_answer", "")
    model_answer = body.get("model_answer", "")
    judge = body.get("judge", {})

    if not judge or not judge.get("base_url") or not judge.get("api_keys"):
        return web.json_response({"correct": False, "reason": "裁判 provider 未配置"}, status=400)

    base_url = judge["base_url"]
    api_key = judge["api_keys"][0] if judge.get("api_keys") else ""
    model = judge.get("model", "")
    jtype = judge.get("type", "openai")
    jtimeout = judge.get("timeout", 30)

    judge_prompt = f"""请判断以下题目模型回答是否正确。

题目：{question}

标准答案：{standard_answer}

模型回答：{model_answer}

判断规则：
1. 语义等价即判对（如 "45" vs "40元现金加一包5元零食" vs "小明赚45店员亏45" 都算对）
2. 数字错误判错
3. 答非所问或空回复判错
4. 方向一致且金额正确判对

只回复 JSON：{{"correct": true/false, "reason": "一句话说明"}}"""

    timeout = aiohttp.ClientTimeout(total=jtimeout)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if jtype == "openai":
                result = await validate_openai(session, base_url, api_key, model, "judge", stream=False, timeout=jtimeout, prompt_text=judge_prompt)
            elif jtype == "anthropic":
                result = await validate_anthropic(session, base_url, api_key, model, "judge", stream=False, timeout=jtimeout, prompt_text=judge_prompt)
            else:
                return web.json_response({"correct": False, "reason": f"不支持协议: {jtype}"}, status=400)

            content = result.get("content", "")
            if not content:
                return web.json_response({"correct": False, "reason": "裁判模型无回复", "elapsed": result.get("elapsed")})

            # 尝试从裁判回复里解析 JSON
            import re
            json_match = re.search(r'\{[^}]*"correct"[^}]*\}', content, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                    return web.json_response({
                        "correct": bool(parsed.get("correct", False)),
                        "reason": parsed.get("reason", ""),
                        "judge_raw": content,
                        "elapsed": result.get("elapsed")
                    })
                except json.JSONDecodeError:
                    pass

            # fallback: 启发式判定裁判回复是否包含 true/false
            lower = content.lower()
            if '"correct": true' in lower or '"correct":true' in lower:
                return web.json_response({"correct": True, "reason": "裁判判定正确", "judge_raw": content, "elapsed": result.get("elapsed")})
            if '"correct": false' or '"correct":false' in lower:
                return web.json_response({"correct": False, "reason": "裁判判定错误", "judge_raw": content, "elapsed": result.get("elapsed")})

            # 无法解析，返回原文让前端自行判断
            return web.json_response({
                "correct": False,
                "reason": f"裁判回复无法解析: {content[:200]}",
                "judge_raw": content,
                "elapsed": result.get("elapsed")
            })

    except asyncio.TimeoutError:
        return web.json_response({"correct": False, "reason": "裁判超时"}, status=504)
    except Exception as e:
        return web.json_response({"correct": False, "reason": f"裁判调用失败: {e}"}, status=500)


async def handle_save_quiz_result(request):
    """前端智测评分后持久化 multi_results 中的 quiz_correct/quiz_reason"""
    body = await request.json()
    name = body.get("name", "")
    quiz_correct = body.get("quiz_correct")
    quiz_reason = body.get("quiz_reason", "")
    quiz_score_summary = body.get("quiz_score_summary", "")
    multi_results = body.get("multi_results")
    cfg = load_config()
    providers = cfg.get("providers", [])
    for p in providers:
        if p.get("name", "") == name:
            ls = p.get("last_status") or {}
            mrs = ls.get("multi_results") or []
            if not mrs and multi_results:
                mrs = multi_results
                ls["multi_results"] = multi_results
            if mrs:
                mrs[0]["quiz_correct"] = quiz_correct
                mrs[0]["quiz_reason"] = quiz_reason
            ls["quiz_score_summary"] = quiz_score_summary
            p["last_status"] = ls
            save_config(cfg)
            return web.json_response({"ok": True})
    return web.json_response({"ok": False, "error": "provider not found"}, status=404)


app.router.add_get("/static/{tail:.*}", handle_static)
app.router.add_get("/api/config", handle_get_config)
app.router.add_post("/api/config", handle_save_config)
app.router.add_post("/api/fetch-models", handle_fetch_models)
app.router.add_post("/api/fetch-all-models", handle_fetch_all_models)
app.router.add_post("/api/validate", handle_validate)
app.router.add_post("/api/validate-all", handle_validate_all)
app.router.add_post("/api/cancel-key", handle_cancel_key)
app.router.add_get("/api/validate-status", handle_validate_status)
app.router.add_post("/api/judge", handle_judge)
app.router.add_post("/api/judge-batch", handle_judge_batch)
app.router.add_post("/api/save-quiz-result", handle_save_quiz_result)
app.router.add_post("/api/select-model", handle_select_model)
app.router.add_post("/api/select-provider", handle_select_provider)
app.router.add_post("/api/stream", handle_stream)
app.router.add_post("/api/delete-provider", handle_delete_provider)
app.router.add_get("/api/logs", handle_get_logs)
app.router.add_post("/api/clear-logs", handle_clear_logs)

if __name__ == "__main__":
    print("🐱 API Key Validator 启动在 http://0.0.0.0:8899")
    web.run_app(app, host="0.0.0.0", port=8899)
