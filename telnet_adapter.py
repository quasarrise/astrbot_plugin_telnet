import asyncio
from astrbot.api.platform import Platform, AstrBotMessage, MessageMember, PlatformMetadata, MessageType
from astrbot.api.message_components import Plain
from astrbot.api.platform import register_platform_adapter
from astrbot.api import logger
from .telnet_event import TelnetMessageEvent
from .bbs_render import (
    R, B, D, C_BLACK, C_RED, C_GREEN, C_YELLOW, C_BLUE,
    C_MAGENTA, C_CYAN, C_WHITE,
    SCREEN_W, CONTENT_W,
    _clr, _eol, _center,
    render_banner, render_chat_intro, render_about,
    render_help, render_announcements,
)


# ═══════════════════════════════════════════════
# Adapter
# ═══════════════════════════════════════════════

@register_platform_adapter(
    "telnet",
    "Telnet适配器",
    default_config_tmpl={
        "监听地址": "0.0.0.0",
        "端口": 2323,
        "编码": "gbk",
        "连接密码（留空则不验证）": "",
    },
)
class TelnetAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.config = platform_config
        self.host = self.config.get("监听地址", self.config.get("host", "0.0.0.0"))
        self.port = int(self.config.get("端口", self.config.get("port", 2323)))
        self.encoding = self.config.get("编码", self.config.get("encoding", "gbk")).lower()
        self.password = self.config.get("连接密码（留空则不验证）", self.config.get("password", "")).strip()

        logger.info(
            f"[Telnet] BBS mode loaded: Host={self.host}, Port={self.port}, "
            f"Encoding={self.encoding}"
        )

    def meta(self) -> PlatformMetadata:
        return PlatformMetadata(
            id="telnet",
            name="Telnet Gateway",
            description="BBS-style telnet gateway for vintage terminals",
            logo_path="./logo.png",
            support_streaming_message=False,
        )

    # ── helpers ─────────────────────────────────

    def _write(self, writer, text: str):
        writer.write(text.encode(self.encoding, errors="replace"))

    def _writeln(self, writer, text: str):
        writer.write((text + _eol()).encode(self.encoding, errors="replace"))

    async def _flush(self, writer):
        await writer.drain()

    async def _wait_key(self, reader, writer):
        """Wait for any key press and discard it (with IAC filtering)."""
        while True:
            b = await reader.read(1)
            if not b:
                return
            if b == b"\xff":
                await self._filter_iac(reader, writer)
                continue
            return

    # ── IAC negotiation ─────────────────────────

    async def _filter_iac(self, reader, writer=None):
        """Parse and respond to incoming Telnet IAC negotiations."""
        cmd = await reader.read(1)
        if not cmd:
            return
        opt = await reader.read(1)
        if not opt:
            return
        opt_byte = ord(opt)
        if opt_byte == 1:  # ECHO
            if cmd == b"\xfd":  # DO
                if writer:
                    writer.write(b"\xff\xfb\x01")  # WILL ECHO
            elif cmd == b"\xfb":  # WILL (client says it will echo locally)
                if writer:
                    writer.write(b"\xff\xfe\x01")  # DONT ECHO
                    writer.write(b"\xff\xfb\x01")  # WILL ECHO
        elif opt_byte == 3:  # SUPPRESS_GO_AHEAD
            if cmd == b"\xfd":  # DO
                if writer:
                    writer.write(b"\xff\xfb\x03")  # WILL SUPPRESS_GO_AHEAD

    # ── password auth ───────────────────────────

    async def _authenticate(self, reader, writer) -> bool:
        if not self.password:
            self._write(writer, C_GREEN + _center("Welcome, stranger!") + R + _eol())
            await self._flush(writer)
            return True
        self._write(writer, C_YELLOW + "Password: " + R)
        await self._flush(writer)
        pwd = ""
        while True:
            b = await reader.read(1)
            if not b:
                return False
            if b == b"\xff":
                await self._filter_iac(reader, writer)
                continue
            if b in (b"\r", b"\n"):
                break
            if b in (b"\x08", b"\x7f"):
                pwd = pwd[:-1]
                continue
            try:
                pwd += b.decode(self.encoding)
            except UnicodeDecodeError:
                continue
        if pwd.strip() != self.password:
            self._writeln(writer, C_RED + "\rAccess denied." + R)
            await self._flush(writer)
            return False
        self._writeln(writer, C_GREEN + "\rOK." + R)
        await self._flush(writer)
        return True

    # ── process to LLM ──────────────────────────

    async def process_to_llm(self, msg_str: str, writer):
        try:
            peer = writer.get_extra_info("peername")
            client_ip = str(peer[0])
            abm = AstrBotMessage()
            abm.type = MessageType.FRIEND_MESSAGE
            abm.message_str = msg_str
            abm.message = [Plain(text=msg_str)]
            abm.sender = MessageMember(user_id=client_ip, nickname=f"User_{peer[1]}")
            abm.session_id = client_ip
            abm.message_id = str(int(asyncio.get_event_loop().time()))

            event = TelnetMessageEvent(
                message_str=msg_str,
                message_obj=abm,
                platform_meta=self.meta(),
                session_id=abm.session_id,
                client_writer=writer,
                encoding=self.encoding,
            )
            self.commit_event(event)
        except Exception as e:
            logger.error(f"[Telnet] Process to LLM Error: {e}")

    # ── main connection handler ─────────────────

    async def handle_client(self, reader, writer):
        # IAC proactive negotiation
        writer.write(b"\xff\xfb\x01")  # WILL ECHO
        writer.write(b"\xff\xfb\x03")  # WILL SUPPRESS_GO_AHEAD
        await self._flush(writer)

        # Welcome
        self._write(writer, C_GREEN + f"Connected. [{self.encoding.upper()} Mode]" + R + _eol() * 2)
        await self._flush(writer)

        # Auth
        if not await self._authenticate(reader, writer):
            writer.close()
            await writer.wait_closed()
            return

        # Enter BBS menu loop
        await self._menu_loop(reader, writer)

        writer.close()
        await writer.wait_closed()

    async def _menu_loop(self, reader, writer):
        """BBS main menu loop."""
        while True:
            self._write(writer, render_banner())
            await self._flush(writer)

            b = await reader.read(1)
            if not b:
                break
            if b == b"\xff":
                await self._filter_iac(reader, writer)
                continue

            try:
                key = b.decode(self.encoding).strip().lower()
            except UnicodeDecodeError:
                continue

            if key == "1":
                await self._chat_loop(reader, writer)
            elif key == "2":
                self._write(writer, render_announcements())
                await self._flush(writer)
                await self._wait_key(reader, writer)
            elif key == "3":
                self._write(writer, render_about())
                await self._flush(writer)
                await self._wait_key(reader, writer)
            elif key == "4":
                self._write(writer, render_help())
                await self._flush(writer)
                await self._wait_key(reader, writer)
            elif key == "5":
                self._writeln(writer, C_YELLOW + "\rGoodbye!" + R)
                await self._flush(writer)
                break

    async def _chat_loop(self, reader, writer):
        """BBS-style chat mode."""
        self._write(writer, render_chat_intro())
        await self._flush(writer)

        input_str = ""
        while True:
            try:
                b = await reader.read(1)
                if not b:
                    return  # disconnected

                if b == b"\xff":
                    await self._filter_iac(reader, writer)
                    continue

                # Backspace
                if b in (b"\x08", b"\x7f"):
                    if input_str:
                        input_str = input_str[:-1]
                        writer.write(b"\x08 \x08")
                        await self._flush(writer)
                    continue

                # Enter
                if b in (b"\r", b"\n"):
                    cmd = input_str.strip()
                    input_str = ""

                    if not cmd:
                        self._write(writer, _eol() + C_GREEN + "[You]" + R + " > ")
                        await self._flush(writer)
                        continue

                    # Built-in commands
                    if cmd == "/menu":
                        return  # back to menu
                    if cmd == "/help":
                        self._write(writer, render_help())
                        await self._flush(writer)
                        await self._wait_key(reader, writer)
                        self._write(writer, render_chat_intro())
                        await self._flush(writer)
                        continue
                    if cmd == "/clear":
                        self._write(writer, render_chat_intro())
                        await self._flush(writer)
                        continue

                    # Send to LLM
                    self._write(
                        writer,
                        _eol() + C_YELLOW + D + "[System] Message sent, waiting..." + R + _eol(),
                    )
                    await self._flush(writer)
                    await self.process_to_llm(cmd, writer)
                    # AI response + prompt arrives via TelnetMessageEvent.send()
                    continue

                # Normal character
                try:
                    char_text = b.decode(self.encoding)
                except UnicodeDecodeError:
                    char_bytes = b
                    while len(char_bytes) < 5:
                        more = await reader.read(1)
                        if not more:
                            break
                        if more == b"\xff":
                            await self._filter_iac(reader, writer)
                            continue
                        char_bytes += more
                        try:
                            char_text = char_bytes.decode(self.encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        self._write(writer, "?")
                        await self._flush(writer)
                        continue

                input_str += char_text
                writer.write(char_text.encode(self.encoding, errors="ignore"))
                await self._flush(writer)

            except Exception as e:
                logger.error(f"[Telnet] Chat loop error: {e}")
                return

    # ── server run ──────────────────────────────

    async def run(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        async with server:
            await server.serve_forever()
