"""A stand-in for a server-reported statement-timeout cancellation."""


class TimedOutStatement(Exception):
    """Shaped as the storage driver raises a statement the server's
    ``statement_timeout`` cancelled: SQLSTATE 57014 and the server's primary
    message naming the timeout."""

    sqlstate = "57014"

    def __init__(self) -> None:
        super().__init__("canceling statement due to statement timeout")
        self.diag = type("Diag", (), {"message_primary": str(self)})()
