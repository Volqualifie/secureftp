"""MySQL 访问层。所有 SQL 都使用参数化查询（%s 占位符），杜绝 SQL 注入。

PyMySQL 是同步库，在 asyncio 中通过 asyncio.to_thread 调用，避免阻塞事件循环。
每次操作使用独立短连接，逻辑简单；后续如有性能需要再换连接池。
"""
import asyncio

import pymysql


class UserExists(Exception):
    pass


class Database:
    def __init__(self, cfg: dict):
        self._cfg = cfg

    def _connect(self):
        return pymysql.connect(
            host=self._cfg["host"], port=int(self._cfg["port"]),
            user=self._cfg["user"], password=str(self._cfg["password"]),
            database=self._cfg["name"], charset="utf8mb4",
            connect_timeout=5, autocommit=True,
        )

    # ---- 同步实现 ----
    def _create_user(self, username: str, password_hash: str) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                        (username, password_hash),
                    )
                except pymysql.err.IntegrityError:
                    raise UserExists(username) from None
                return cur.lastrowid
        finally:
            conn.close()

    def _get_user(self, username: str):
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    "SELECT id, username, password_hash, is_vip FROM users WHERE username = %s",
                    (username,),
                )
                return cur.fetchone()
        finally:
            conn.close()

    def _log_transfer(self, user_id, action, filename, size, sha256, status):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO transfer_logs (user_id, action, filename, size, sha256, status) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (user_id, action, filename, size, sha256, status),
                )
        finally:
            conn.close()

    def _set_vip(self, user_id: int) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE users SET is_vip = 1 WHERE id = %s", (user_id,))
        finally:
            conn.close()

    # ---- 异步接口（供 handlers 调用） ----
    async def create_user(self, username, password_hash):
        return await asyncio.to_thread(self._create_user, username, password_hash)

    async def get_user(self, username):
        return await asyncio.to_thread(self._get_user, username)

    async def log_transfer(self, user_id, action, filename, size, sha256, status):
        return await asyncio.to_thread(
            self._log_transfer, user_id, action, filename, size, sha256, status)

    async def set_vip(self, user_id):
        return await asyncio.to_thread(self._set_vip, user_id)