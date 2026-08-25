# 模型配置

项目支持两种运行时模型后端：

- `extractive`：默认值。直接返回已授权证据，不需要网络或模型密钥，适用于测试和本地开发。
- `openai_compatible`：调用兼容 OpenAI Chat Completions 接口的远端模型，适用于生产或联调。

## OpenAI 兼容模式

在 `.env` 中填写以下配置；密钥只通过 `env://` 引用，不写入助手版本记录。
开发环境会优先读取进程环境变量，未设置时读取项目根目录的 `.env`；生产环境建议仅由部署平台
注入环境变量或外部密钥管理器提供密钥。

```dotenv
RAG_MODEL_BACKEND=openai_compatible
RAG_MODEL_API_BASE=https://api.example.com/v1
RAG_MODEL_NAME=example-chat-model
RAG_MODEL_API_KEY_REFERENCE=env://MODEL_API_KEY
MODEL_API_KEY=replace-with-a-real-secret
```

兼容端点必须提供 `POST /chat/completions`，并返回 `choices[0].message.content`。若响应中存在
`usage.prompt_tokens` 和 `usage.completion_tokens`，系统会将其写入遥测指标。

## DeepSeek 示例

DeepSeek 使用 OpenAI 兼容接口，可以采用下列环境变量名称：

```dotenv
RAG_MODEL_BACKEND=openai_compatible
RAG_MODEL_API_BASE=https://api.deepseek.com/v1
RAG_MODEL_NAME=deepseek-chat
RAG_MODEL_API_KEY_REFERENCE=env://DEEPSEEK_API_KEY
DEEPSEEK_API_KEY=replace-with-a-real-secret
```

## 运行时行为

启动时会校验远端模式是否具有端点地址、模型名称和 `env://` 密钥引用。调用过程受
`RAG_REQUEST_TIMEOUT_SECONDS`、`RAG_PROVIDER_RETRY_ATTEMPTS`、
`RAG_QUERY_RATE_LIMIT_PER_MINUTE` 和 `RAG_QUERY_CONCURRENCY_LIMIT` 约束。

开发环境中，远端模型网关保留本地抽取式模型作为可选回退；生产环境不自动回退，避免将远端
模型故障误判为真实生成结果。
