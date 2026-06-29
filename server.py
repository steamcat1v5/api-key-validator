#!/usr/bin/env python3
"""API Key Validator — 后端 API v2"""

import yaml
import json
import asyncio
import aiohttp
import time
from pathlib import Path
from aiohttp import web

CONFIG_PATH = Path(__file__).parent / "config.yml"


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        providers = cfg.get("providers", [])
        for p in providers:
            p.setdefault("models", [])
            p.setdefault("selected_model", "")
            p.setdefault("source_url", "")
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
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    req_log = f"─── Request ───\nGET {url}\n{fmt_headers(headers)}"

    try:
        start = time.time()
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            elapsed = time.time() - start
            body = await resp.text()
            status = resp.status
            try:
                body_json = json.loads(body)
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
            except:
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

        log = {"provider": provider_name, "method": "GET", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

        if status == 200:
            models = sorted([m["id"] for m in body_json.get("data", [])])
            return {"ok": True, "models": models, "log": log}
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


async def validate_openai(session, base_url, api_key, model, provider_name, stream=False):
    """OpenAI 协议: POST /v1/chat/completions"""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 50}
    if stream:
        payload["stream"] = True

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    try:
        start = time.time()
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
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
                # 流式：检查第一个 data: 行
                for line in body.strip().split("\n"):
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            usage = chunk.get("usage", {})
                            return {
                                "ok": True, "status": "available", "model": model,
                                "stream": True, "usage": usage,
                                "log": log,
                            }
                        except:
                            pass
                return {"ok": True, "status": "available", "model": model, "stream": True, "log": log}
            else:
                try:
                    body_json = json.loads(body)
                    resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
                except:
                    resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

                log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

                if status == 200:
                    usage = body_json.get("usage", {})
                    content = ""
                    choices = body_json.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "")[:80]
                    return {
                        "ok": True, "status": "available", "model": model,
                        "stream": False, "usage": usage, "content": content,
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
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout (30s)"}
        return {"ok": False, "status": "timeout", "model": model, "log": log}
    except Exception as e:
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n❌ {e}"}
        return {"ok": False, "status": "error", "model": model, "error": str(e), "log": log}


async def validate_anthropic(session, base_url, api_key, model, provider_name):
    """Anthropic 协议: POST /v1/messages"""
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    payload = {"model": model, "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}

    req_log = f"─── Request ───\nPOST {url}\n{fmt_headers(headers)}\n\n{fmt_json(payload)}"

    try:
        start = time.time()
        async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            elapsed = time.time() - start
            body = await resp.text()
            status = resp.status
            try:
                body_json = json.loads(body)
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{fmt_json(body_json)}"
            except:
                resp_log = f"─── Response ({elapsed:.2f}s) ───\nHTTP {status}\n{body[:500]}"

            log = {"provider": provider_name, "method": "POST", "url": url, "status": str(status), "detail": f"{req_log}\n\n{resp_log}"}

            if status == 200:
                usage = body_json.get("usage", {})
                content = ""
                content_arr = body_json.get("content", [])
                if content_arr:
                    content = content_arr[0].get("text", "")[:80]
                return {"ok": True, "status": "available", "model": model, "usage": usage, "content": content, "log": log}
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
        log = {"provider": provider_name, "method": "POST", "url": url, "status": "0", "detail": f"{req_log}\n\n─── Response ───\n⏱ Timeout (30s)"}
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
    return web.json_response({"providers": providers, "stream": cfg.get("stream", False)})


async def handle_save_config(request):
    body = await request.json()
    new_providers = body.get("providers", [])
    old_cfg = load_config()
    old_providers = old_cfg.get("providers", [])
    old_key_map = {(p.get("name", ""), p.get("base_url", "")): p.get("api_key", "") for p in old_providers}

    merged = []
    for p in new_providers:
        key = p.get("api_key", "")
        if not key or "***" in key:
            lookup_key = (p.get("name", ""), p.get("base_url", ""))
            key = old_key_map.get(lookup_key, "")
        merged.append({
            "name": p.get("name", ""),
            "type": p.get("type", "openai"),
            "base_url": p.get("base_url", ""),
            "api_key": key,
            "models": p.get("models", []),
            "selected_model": p.get("selected_model", ""),
            "source_url": p.get("source_url", ""),
        })
    try:
        save_config({"providers": merged, "stream": body.get("stream", False)})
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
    api_key = body.get("api_key", "")
    ptype = body.get("type", "openai")

    # 优先用前端传来的完整信息，凑不齐再从 config 查
    cfg = load_config()
    providers = cfg.get("providers", [])
    provider = next((p for p in providers if p["name"] == name), None)

    if not provider and not (base_url and api_key):
        return web.json_response({"error": "provider not found (需要先保存或传入 base_url + api_key)"}, status=404)

    # 合并：config 里的作为基础，前端传来的字段覆盖
    if provider:
        base_url = base_url or provider.get("base_url", "")
        api_key = api_key or provider.get("api_key", "")
        ptype = ptype if ptype != "openai" or provider.get("type") else provider.get("type", "openai")
    else:
        # 新 provider 不在 config 中，先插入到 config 以便后续保存模型列表
        provider = {"name": name, "type": ptype, "base_url": base_url, "api_key": api_key,
                     "models": [], "selected_model": "", "source_url": body.get("source_url", "")}
        providers.append(provider)
        cfg["providers"] = providers

    # key 脱敏还原：如果前端传来带 *** 的 key，从 config 里取真实值
    if "***" in api_key and provider:
        api_key = provider.get("api_key", api_key)

    logs = []
    async with aiohttp.ClientSession() as session:
        if ptype == "openai":
            result = await fetch_models_openai(session, base_url, api_key, name)
            logs.append(result["log"])
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
                result = await fetch_models_openai(session, p["base_url"], p["api_key"], p["name"])
                all_logs.append(result["log"])
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
    api_key = body.get("api_key", "")
    model = body.get("model", "")  # 前端可直接传模型名
    ptype = body.get("type", "openai")

    cfg = load_config()
    providers = cfg.get("providers", [])
    provider = next((p for p in providers if p["name"] == name), None)

    if not provider and not (base_url and api_key):
        return web.json_response({"error": "provider not found (需要先保存或传入 base_url + api_key)"}, status=404)

    if provider:
        base_url = base_url or provider.get("base_url", "")
        api_key = api_key or provider.get("api_key", "")
        ptype = ptype or provider.get("type", "openai")
        model = model or provider.get("selected_model", "")
        if "***" in api_key:
            api_key = provider.get("api_key", api_key)
    else:
        # 不在 config 中且前端没传 model → 报错
        if not model:
            return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    if not model:
        return web.json_response({"ok": False, "error": "请先获取模型列表并选择一个模型", "logs": []})

    logs = []
    async with aiohttp.ClientSession() as session:
        if ptype == "openai":
            result = await validate_openai(session, base_url, api_key, model, name, stream=stream)
            logs.append(result.get("log", {}))
            return web.json_response({**result, "logs": logs})
        elif ptype == "anthropic":
            result = await validate_anthropic(session, base_url, api_key, model, name)
            logs.append(result.get("log", {}))
            return web.json_response({**result, "logs": logs})
        else:
            return web.json_response({"ok": False, "error": f"不支持的类型: {ptype}", "logs": []})


async def handle_validate_all(request):
    """批量验证所有 provider"""
    body = await request.json()
    stream = body.get("stream", False)
    cfg = load_config()
    providers = cfg.get("providers", [])
    all_logs = []
    results = []

    async with aiohttp.ClientSession() as session:
        for p in providers:
            model = p.get("selected_model", "")
            if not model:
                results.append({"name": p.get("name", ""), "ok": False, "status": "no_model", "error": "未选择模型"})
                continue
            if p["type"] == "openai":
                result = await validate_openai(session, p["base_url"], p["api_key"], model, p["name"], stream=stream)
                all_logs.append(result.get("log", {}))
                results.append({**result, "name": p["name"]})
            elif p["type"] == "anthropic":
                result = await validate_anthropic(session, p["base_url"], p["api_key"], model, p["name"])
                all_logs.append(result.get("log", {}))
                results.append({**result, "name": p["name"]})

    return web.json_response({"results": results, "logs": all_logs})


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


async def handle_stream(request):
    """保存 stream 设置"""
    body = await request.json()
    stream = body.get("stream", False)
    cfg = load_config()
    cfg["stream"] = stream
    save_config(cfg)
    return web.json_response({"ok": True, "stream": stream})


app = web.Application()
app.router.add_get("/", handle_index)
app.router.add_get("/api/config", handle_get_config)
app.router.add_post("/api/config", handle_save_config)
app.router.add_post("/api/fetch-models", handle_fetch_models)
app.router.add_post("/api/fetch-all-models", handle_fetch_all_models)
app.router.add_post("/api/validate", handle_validate)
app.router.add_post("/api/validate-all", handle_validate_all)
app.router.add_post("/api/select-model", handle_select_model)
app.router.add_post("/api/stream", handle_stream)

if __name__ == "__main__":
    print("🐱 API Key Validator 启动在 http://0.0.0.0:8899")
    web.run_app(app, host="0.0.0.0", port=8899)
