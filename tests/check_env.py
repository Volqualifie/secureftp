#自检脚本
import ssl, pymysql, bcrypt, yaml, PySide6

cfg = yaml.safe_load(open("server/config.yaml", encoding="utf-8"))

# 1. 数据库
db = cfg["database"]
conn = pymysql.connect(host=db["host"], port=db["port"], user=db["user"],
                       password=db["password"], database=db["name"])
cur = conn.cursor(); cur.execute("SHOW TABLES"); print("数据库表:", cur.fetchall())

# 2. 证书能被 TLS 加载
ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
ctx.load_cert_chain("certs/server-cert.pem", "certs/server-key.pem")
print("证书加载 OK")

# 3. bcrypt
print("bcrypt OK:", bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt())))
print("PySide6", PySide6.__version__)