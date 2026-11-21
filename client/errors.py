class ClientError(Exception):
    """客户端异常基类。"""


class ConnectError(ClientError):
    """无法建立连接（网络不通、TLS 握手失败、证书不被信任等）。"""


class ConnectionLost(ClientError):
    """已建立的连接中途断开，或当前没有可用连接。传输层据此等待重连并续传。"""


class AuthError(ClientError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class TransferError(ClientError):
    """服务端明确拒绝或校验失败，重试没有意义（冲突类错误除外）。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Cancelled(ClientError):
    """用户取消传输。半成品会保留，下次可以续传。"""