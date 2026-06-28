# 🔑 API Key Validator

批量验证 API Key 有效性的工具，支持 OpenAI 和 Anthropic 兼容协议。

## 功能

- **📋 模型列表获取** — 调用 `/v1/models` 自动获取可用模型
- **🔍 验证** — 用选定模型发送真实请求验证 Key 可用性
- **📝 YAML 配置** — 所有 Provider 信息持久化到 `config.yml`
- **🔒 双协议** — 支持 OpenAI 兼容 (`/v1/chat/completions`) 和 Anthropic (`/v1/messages`)
- **📊 请求日志** — 完整记录请求/响应内容，方便调试
- **🔄 Stream 模式** — 可选流式验证
- **🚀 批量操作** — 一键获取所有 Provider 的模型列表或批量验证

## 快速开始

### 前置要求

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (推荐) 或 pip

### 安装

```bash
git clone git@github.com:steamcat1v5/api-key-validator.git
cd api-key-validator
uv sync
```

### 配置

编辑 `config.yml`，添加你的 Provider：

```yaml
providers:
  - name: "我的 API"
    type: openai              # openai 或 anthropic
    source_url: "https://example.com/get-key"
    base_url: "https://api.example.com/v1"
    api_key: "sk-xxxxxxxx"
    models: []                # 点击「获取模型」自动填充
    selected_model: ""         # 获取模型后自动选择，也可手动输入
```

### 启动

```bash
uv run python server.py
```

访问 http://localhost:8899

## 使用流程

1. **添加 Provider** — 点击「➕ 添加」，填写 Base URL 和 API Key
2. **获取模型** — 点击「📡 获取模型」拉取可用模型列表（自动保存）
3. **选择模型** — 从下拉列表选择或手动输入模型名称
4. **验证** — 点击「🔍 验证」发送真实请求测试 Key 可用性

## 技术栈

- **后端**: Python + aiohttp
- **前端**: 原生 HTML/CSS/JS 单页面应用
- **配置**: YAML (PyYAML)

## License

[MIT](LICENSE)
