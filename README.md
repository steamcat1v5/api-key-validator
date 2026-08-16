# 🔑 API Key Validator

批量验证 API Key 有效性的工具，支持 OpenAI 和 Anthropic 兼容协议。

## 功能

- **📋 模型列表获取** — 调用 `/v1/models` 自动获取可用模型
- **🔍 验证** — 用选定模型发送真实请求验证 Key 可用性
- **🔑 多 Key 管理** — 每个 Provider 支持多个 API Key，逐条验证或批量测试
- **📝 配置持久化** — Provider 配置持久化到 `config.yml`，验证结果独立存储到 `results/` 目录
- **🔒 双协议** — 支持 OpenAI 兼容 (`/v1/chat/completions`) 和 Anthropic (`/v1/messages`)
- **📊 请求日志** — 完整记录请求/响应内容，方便调试
- **🧠 智能评分** — 用 LLM 裁判对验证回复进行智测评分（可选）
- **🔄 Stream 模式** — 可选流式验证
- **🚀 批量操作** — 一键获取所有 Provider 的模型列表或批量验证
- **🌐 代理支持** — 通过代理验证需要网络代理才能访问的 API

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
    api_keys:                 # 支持多个 Key
      - "sk-xxxxxxxx"
      - "sk-yyyyyyyy"
    models: []                # 点击「获取模型」自动填充
    selected_model: ""        # 获取模型后自动选择，也可手动输入
    timeout: 60               # 请求超时秒数
    extra_headers: {}         # 额外的请求头
```

### 存储结构

| 路径 | 内容 | 说明 |
|---|---|---|
| `config.yml` | Provider 配置、代理设置 | 仅存配置项，不含运行数据 |
| `results/` | 验证/智测结果 | `{provider_name}.json`，含 `last_status` 和 `multi_results` |
| `logs/` | 请求/响应日志 | 每 Provider 按日期滚动，最多保留 3 个文件 |
| `backups/` | config.yml 备份 | 保存配置时自动生成 |

### 启动

```bash
uv run python server.py
```

访问 http://localhost:8899

## 使用流程

1. **添加 Provider** — 点击「➕ 添加」，填写 Base URL 和 API Key（支持多个 Key 逐行填写）
2. **获取模型** — 点击「📡 获取模型」拉取可用模型列表（自动保存）
3. **选择模型** — 从下拉列表选择或手动输入模型名称
4. **验证** — 点击「🔍 验证」发送真实请求测试 Key 可用性
   - 多 Key 时可逐条验证或点击「全部验证」批量测试
   - 验证结果实时更新到表格中（available / error / timeout / cancelled）
5. **智测评分** — 点击「🧠 智测」让 LLM 裁判对回复质量打分
6. **查看详情** — 点击「详情」查看完整的请求/响应日志

## 部署

### systemd

```bash
# 创建 systemd service 文件
cat > /etc/systemd/system/api-key-validator.service << 'EOF'
[Unit]
Description=API Key Validator
After=network.target

[Service]
WorkingDirectory=/path/to/api-key-validator
ExecStart=/path/to/uv run python server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl enable --now api-key-validator
```

## 技术栈

- **后端**: Python + aiohttp
- **前端**: 原生 HTML/CSS/JS 单页面应用
- **配置**: YAML (PyYAML)

## License

[MIT](LICENSE)
