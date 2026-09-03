from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain

try:
    from astrbot.api import logger
except ImportError:  # fallback for environments without astrbot.api.logger
    import logging

    logger = logging.getLogger(__name__)

SEP = "─" * 44

# ── ANSI / line-control constants ──────────────
C_RESET = "\x1b[0m"
C_GREEN = "\x1b[32m"
C_CYAN = "\x1b[36m"
C_YELLOW = "\x1b[33m"
C_RED = "\x1b[31m"
R = "\x1b[0m"


def _eol() -> str:
    return "\r\n"


def _disp_width(ch: str) -> int:
    """显示列宽：CJK 全角=2 列，其余=1 列（覆盖东亚表意/全角标点区）。"""
    o = ord(ch)
    if (0x2E80 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF):
        return 2
    return 1


def _safe_wrap(text: str, width: int, first_prefix: int = 0) -> str:
    """CJK 安全换行：按显示列宽折行，全角字永不跨行被劈开（字节型终端的乱码根因）。

    width = 显示列宽（全角=2 列，如标准 VGA 文本模式 80 列 = 40 个汉字）。
    first_prefix = 首行文本之前的行首前缀列数（如 `[AI] ` 占 5 列，
    首行实际可用列 = width - first_prefix），否则 `[AI] `+text 叠加超宽
    会触发终端在右缘劈字。仅首逻辑行扣除前缀，换行后用满宽。
    """
    lines = text.split("\n")
    out = []
    prefix = first_prefix
    for ln in lines:
        avail = width - prefix
        col = 0
        for ch in ln:
            w = _disp_width(ch)
            if col + w > avail:
                out.append("\n")
                col = 0
                avail = width  # 折行起的后续行用满宽
            out.append(ch)
            col += w
        out.append("\n")
        prefix = 0  # 前缀只作用于第一逻辑行
    return "".join(out).rstrip("\n")


def _pad_cjk(text: str) -> str:
    """在每个 CJK 全角字符后补一个空格。

    给按半宽渲染汉字的客户端（如 Mocha/WM6）留出完整宽度，避免汉字挤在一起。
    仅用于兼容这类特殊客户端；正常 80 列终端开这个会浪费一半行宽。
    """
    out = []
    for ch in text:
        out.append(ch)
        if _disp_width(ch) == 2:
            out.append(" ")
    return "".join(out)


class TelnetMessageEvent(AstrMessageEvent):
    def __init__(self, message_str: str, message_obj: AstrBotMessage,
                 platform_meta: PlatformMetadata, session_id: str,
                 client_writer, encoding: str, wrap_width: int = 80,
                 cjk_pad: bool = False):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client_writer = client_writer
        self.encoding = encoding
        self.wrap_width = wrap_width
        self.cjk_pad = cjk_pad

    async def send(self, message: MessageChain):
        try:
            for element in message.chain:
                if isinstance(element, Plain):
                    text = element.text
                    if self.cjk_pad:
                        text = _pad_cjk(text)  # 半宽 CJK 客户端：字后补空格
                    # ``[AI] `` 行首前缀占 5 列，首行按 wrap_width-5 折行防劈字
                    text = _safe_wrap(
                        text, self.wrap_width, first_prefix=5
                    ).replace("\n", "\r\n")

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