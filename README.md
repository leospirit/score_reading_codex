# 英语朗读评测系统（Docker 一键部署）

## 快速启动（Windows）
1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)
2. 双击 `启动.bat`
3. 浏览器访问 `http://localhost`

首次运行会自动构建镜像（约 5-10 分钟）。  
`.env` 已预置为空 key，可先启动，再按需填写。

## API Key 配置
编辑根目录 `.env`：

```env
OPENAI_API_KEY=
GEMINI_API_KEY=
GEMINI_API_BASE=
AZURE_API_KEY=
```

说明：
- 可全部留空（可启动，但云端能力会受限）
- 可只填你要用的一个或多个 key
- 多个 Gemini key 用英文逗号分隔

## 常用命令
- 启动：`启动.bat`
- 停止：`停止.bat`
- 日志：`查看日志.bat`

或手动执行：

```bash
docker compose up -d --build
docker compose down
docker compose logs -f
```

## 端口
- Web: `80`
- API: `8000`

## 目录说明
- `docker-compose.yml`: 编排
- `Dockerfile.api`: 后端镜像
- `Dockerfile.web`: 前端镜像
- `score_reading/`: 核心评分引擎
- `data/`: 上传与结果目录
