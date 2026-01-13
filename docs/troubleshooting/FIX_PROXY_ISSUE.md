# 修复代理配置问题

## 问题

运行 `uv run data-alchemy ingest` 时遇到错误：

```
ValueError: Unknown scheme for proxy URL URL('socks://127.0.0.1:7890/')
```

## 原因

- FIClash 默认使用 `socks://` 协议
- `httpx`（OpenAI 客户端使用的 HTTP 库）只支持 `http://` 和 `https://` 代理，不支持 `socks://`

## 解决方案

已创建 `src/utils/proxy.py` 工具模块，自动处理代理配置：

1. **自动转换**：将 `socks://127.0.0.1:7890` 转换为 `http://127.0.0.1:7890`
2. **过滤 socks 代理**：如果只有 socks 代理，会尝试使用 HTTP 代理或禁用代理
3. **应用到所有 OpenAI 客户端**：已更新 `SFTGenerator`、`AgentC`、`AgentD`

## 修复的文件

- `src/utils/proxy.py` - 新增代理工具模块
- `src/synthesis/sft_generator.py` - 使用代理工具
- `src/agents/agent_c.py` - 使用代理工具
- `src/agents/agent_d.py` - 使用代理工具

## 验证

### 方法 1: 测试代理转换

```bash
python3 -c "
import sys, os
sys.path.insert(0, 'src')
os.environ['ALL_PROXY'] = 'socks://127.0.0.1:7890/'
from utils.proxy import get_http_proxy
print('Converted:', get_http_proxy())
"
# 应该输出: Converted: http://127.0.0.1:7890
```

### 方法 2: 测试 FIClash HTTP 代理

```bash
# 测试 FIClash 是否支持 HTTP 代理
curl -x http://127.0.0.1:7890 https://api.deepseek.com
```

### 方法 3: 运行完整流程

```bash
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```

## 如果 FIClash 不支持 HTTP 代理

如果 FIClash 只支持 SOCKS 代理，可以：

### 选项 1: 临时禁用代理（仅对 OpenAI 调用）

修改 `src/utils/proxy.py` 中的 `get_http_proxy()` 函数，直接返回 `None`：

```python
def get_http_proxy() -> Optional[str]:
    # 如果只有 socks 代理，禁用代理
    return None
```

### 选项 2: 配置 FIClash 同时提供 HTTP 代理

在 FIClash 配置中启用 HTTP 代理端口（通常是 7890）。

### 选项 3: 使用环境变量覆盖

在运行命令前临时取消代理：

```bash
unset ALL_PROXY
unset all_proxy
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```

## 当前状态

- ✅ 代理转换逻辑已实现
- ✅ 所有 OpenAI 客户端已更新
- ⚠️ 需要验证 FIClash 是否支持 HTTP 代理

## 下一步

运行命令测试：

```bash
uv run data-alchemy ingest --mode spark --synthesis --max_samples 50
```

如果仍有问题，请检查 FIClash 配置或使用选项 3 临时禁用代理。
