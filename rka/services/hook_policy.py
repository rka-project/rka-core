"""One execution policy for new and persisted lifecycle hooks."""

SUPPORTED_HOOK_HANDLERS = frozenset({"brain_notify"})


class UnsupportedHookHandlerError(ValueError):
    """A legacy handler may be inspected, but cannot be registered or run."""

    code = "unsupported_hook_handler"

    def __init__(self, handler_type: str):
        self.handler_type = handler_type
        super().__init__(
            f"Hook handler {handler_type!r} is unsupported; only brain_notify "
            "can be registered, enabled, or executed. Legacy hooks remain readable "
            "and can be disabled."
        )


def require_supported_hook_handler(handler_type: str) -> None:
    if handler_type not in SUPPORTED_HOOK_HANDLERS:
        raise UnsupportedHookHandlerError(handler_type)
