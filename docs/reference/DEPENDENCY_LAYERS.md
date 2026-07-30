# 依赖分层

当前单一 `pyproject.toml` 仍是开发兼容集合；部署镜像不得据此无差别安装训练栈。

| 层 | 运行面 | 允许的依赖 |
| --- | --- | --- |
| online | WebUI、任务网关、PostgreSQL、Redis、MinIO | FastAPI、psycopg、redis、boto3、认证与监控包 |
| retrieval | 在线检索与嵌入 | torch、transformers、sentence-transformers、jieba |
| connector | 同步/ETL job | polars、s3fs、文档解析、Spark |
| training | 训练 job 专用 | peft、datasets、accelerate、torchvision、torchaudio |
| dev | CI 与本地测试 | pytest、pytest-asyncio、pytest-mock、ruff |

Phase 3 的 Helm WebUI 镜像必须采用 `online + retrieval`；训练与 Spark 进入独立 job
镜像。下一次基础镜像重建时将这张表拆为可锁定的 uv extras，避免在未重新验证 ROCm
锁文件的情况下改写生产依赖解析。
