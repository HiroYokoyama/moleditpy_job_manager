class ServerError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def reject(status, code, message):
    raise ServerError(status, code, message)
