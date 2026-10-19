# fixtures

本目录存放 `python -m scripts.recon <key>` 抓取到的**真实对话接口样本**，是各 provider 适配器的工作依据。

- 文件由 recon 自动生成，不手工编辑；`.gitignore` 已忽略 `fixtures/*.json`（含真实接口结构与对话样例）。
- 这些接口/签名算法会随官网更新而失效：**适配器不凭记忆硬编码**，一切以这里抓到的当下流量为准。
- headers 里的敏感值（authorization / cookie / x-signature 等）已被替换为 `{{RUNTIME_INJECT:<头名>}}` 占位符，运行时从实时登录态注入。

## 文件结构（recon 产出 / 适配器读取）

```json
{
  "provider": "qwen",
  "endpoint": "<真实对话接口 URL>",
  "method": "POST",
  "headers": { "content-type": "application/json", "authorization": "{{RUNTIME_INJECT:authorization}}", "...": "..." },
  "body_template": { "...": "抓取时那条样例消息的原始请求体，适配器会把 prompt/会话字段替换成实时值" },
  "response_content_type": "text/event-stream | application/json",
  "response_sample": "<响应体前若干字符，用于调试解析>",
  "candidates": [ { "url": "...", "method": "POST", "status": 200, "content_type": "..." } ]
}
```

## 使用流程

```
python -m scripts.login <key>
python -m scripts.recon <key>     # 在页面里发一条"你好"，回终端按回车
```

若 recon 未能识别到对话端点（`endpoint` 为空），查看 `candidates` 列表，手动把正确 URL 填入 `config.yaml` 对应 provider 的 `endpoint`，或重跑 recon。
