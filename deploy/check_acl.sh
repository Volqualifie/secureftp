#!/bin/bash
# 访问控制验证脚本：在容器内证明服务进程无法越出指定目录。可截图放进实验报告。
cd "$(dirname "$0")/.."
run() { echo; echo "\$ $*"; docker compose exec -T ftp-server sh -c "$*" 2>&1 | head -5; }
echo "== 1. 服务进程身份（应为非 root，uid=10001）"; run 'id'
echo "== 2. 写系统目录（应被拒绝：只读根文件系统 + 非 root）"; run 'touch /app/hack && echo 写入成功'; run 'touch /etc/hack && echo 写入成功'
echo "== 3. 读取敏感文件（证书目录只读挂载，写入应失败）"; run 'touch /certs/x && echo 写入成功'
echo "== 4. 唯一可写目录（应成功）"; run 'touch /data/storage/.probe && echo 写入成功 && rm /data/storage/.probe'
echo "== 5. 能力与权限（CapEff 应为全 0）"; run 'grep -E "CapEff|NoNewPrivs" /proc/self/status'
echo "== 6. 数据库端口未对宿主机发布"; docker compose port mysql 3306 || echo "（无映射，符合预期）"