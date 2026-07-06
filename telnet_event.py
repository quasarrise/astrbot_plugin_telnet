from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain
from astrbot.api import logger

# ANSI color constants (duplicated from adapter to avoid circular import)
R = "\x1b[0m"
B = "\x1b[1m"
D = "\x1b[2m"
C_GREEN = "\x1b[32m"
C_CYAN = "\x1b[36m"
C_YELLOW = "\x1b[33m"
C_WHITE = "\x1b[37m"
C_BRIGHT_GREEN = "\x1b[92m"

SEPARATOR_WIDTH = 44
SEP = "-" * SEPARATOR_WIDTH


def _eol() -> str:
    return "\r\n"


class TelnetMessageEvent(AstrMessageEvent):
    def __init__(self, message_str: str, message_obj: AstrBotMessage,
                 platform_meta: PlatformMetadata, session_id: str,
                 client_writer, encoding: str):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client_writer = client_writer
        self.encoding = encoding

    async def send(self, message: MessageChain):
        try:
            for element in message.chain:
                if isinstance(element, Plain):
                    text = element.text.replace("\n", "\r\n")

                    # ── BBS-styled AI response ──────────────
                    #   ───── [AI] ──────────────────────────
                    #   <response text>
                    #   ─────────────────────────────────────
                    #   [You] >
                    styled_lines = [
                        _eol(),
                        C_CYAN + SEP + R + _eol(),
                        C_GREEN + "[AI]" + R + " " + text + _eol(),
                        C_CYAN + SEP + R + _eol(),
                        C_GREEN + "[You]" + R + " > ",
                    ]
                    output = "".join(styled_lines)

                    if not self.client_writer.transport.is_closing():
                        self.client_writer.write(
                            output.encode(self.encoding, errors="replace")
                        )
                        await self.client_writer.drain()
                    else:
                        logger.warning("[Telnet] Writer closed, message lost.")
                        break

        except Exception as e:
            logger.error(f"[Telnet] Send error: {e}")

        await super().send(message)

    def get_platform_name(self) -> str:
        return "telnet"
