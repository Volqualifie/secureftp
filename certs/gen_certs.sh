#!/bin/bash
set -e
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1   # 防止 Git Bash 改写 /CN=... 路径

# 1. CA
openssl genrsa -out ca-key.pem 4096
openssl req -x509 -new -key ca-key.pem -sha256 -days 3650 \
  -subj "/C=CN/O=SecureFTP/CN=SecureFTP Root CA" -out ca-cert.pem

# 2. 服务端（SAN 必须含 localhost 和 127.0.0.1）
openssl genrsa -out server-key.pem 2048
openssl req -new -key server-key.pem -subj "/C=CN/O=SecureFTP/CN=localhost" -out server.csr
cat > server-ext.cnf << 'EOF'
subjectAltName = DNS:localhost, IP:127.0.0.1
extendedKeyUsage = serverAuth
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in server.csr -CA ca-cert.pem -CAkey ca-key.pem \
  -CAcreateserial -days 365 -sha256 -extfile server-ext.cnf -out server-cert.pem

# 3. 客户端（加分项：双向证书认证）
openssl genrsa -out client-key.pem 2048
openssl req -new -key client-key.pem -subj "/C=CN/O=SecureFTP/CN=client01" -out client.csr
cat > client-ext.cnf << 'EOF'
extendedKeyUsage = clientAuth
basicConstraints = CA:FALSE
EOF
openssl x509 -req -in client.csr -CA ca-cert.pem -CAkey ca-key.pem \
  -CAcreateserial -days 365 -sha256 -extfile client-ext.cnf -out client-cert.pem

rm -f *.csr *.cnf
echo "证书生成完成"