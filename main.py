from astrbot.api.star import Context, Star

class MyPlugin(Star):
    def __init__(self, context: Context):
        from .telnet_adapter import TelnetAdapter # noqa