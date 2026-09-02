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
        # Echo ownership is client-driven (see handle_client): server echoes only when
        # the client asks via DO ECHO. Default OFF so local-echo clients stay single-echo.
        self._server_echo = False

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

    # ── telnet protocol (RFC 854) handling ──────
    # IAC = 0xff. Commands: WILL=0xfb WONT=0xfc DO=0xfd DONT=0xfe
    #                           SB=0xfa  SE=0xf0
    # Options we support: ECHO=1, SGA=3. Everything else is refused.

    async def _read_chunk(self, reader) -> int | None:
        """Read 1 raw byte from the socket; return int value or None on EOF."""
        b = await reader.read(1)
        if not b:
            return None
        return b[0]

    async def _handle_iac(self, reader, writer=None):
        """Parse one full telnet sequence whose leading 0xff was already consumed.
        Always consumes the ENTIRE sequence — including SB...SE sub-negotiations —
        so no negotiation bytes can ever leak into application input. Replies to
        WILL/DO for options the BBS supports."""
        cmd = await self._read_chunk(reader)
        if cmd is None:
            return
        if cmd == 0xfa:  # SB — sub-negotiation, consume until IAC SE
            while True:
                nxt = await self._read_chunk(reader)
                if nxt is None:
                    return
                if nxt == 0xff:  # IAC inside SB: SE (0xf0) ends it, IAC IAC is escaped data
                    nxt2 = await self._read_chunk(reader)
                    if nxt2 is None:
                        return
                    if nxt2 == 0xf0:  # SE
                        return
            # unreachable, kept for clarity
        if cmd in (0xfb, 0xfc, 0xfd, 0xfe):  # WILL / WONT / DO / DONT
            opt = await self._read_chunk(reader)
            if opt is None:
                return
            self._respond_nego(cmd, opt, writer)
            return
        # 0xf0 (stray SE) or any other byte — ignore

    def _respond_nego(self, cmd: int, opt: int, writer):
        """Reply to a client WILL/DO. Echo is client-driven: the server keeps its own
        echo OFF unless the client asks for it (DO ECHO)."""
        if not writer:
            return
        if cmd == 0xfb:  # client WILL <opt>
            if opt == 1:      # ECHO — client will echo locally, server must NOT echo
                self._server_echo = False
                writer.write(b"\xff\xfd\x01")  # DO ECHO (you do it, I won't)
            elif opt == 3:    # SGA
                writer.write(b"\xff\xfd\x03")  # DO SGA
            else:
                writer.write(bytes([0xff, 0xfc, opt]))  # WONT — unsupported
        elif cmd == 0xfd:  # client DO <opt>
            if opt == 1:      # ECHO — client asks the server to echo
                self._server_echo = True
                writer.write(b"\xff\xfb\x01")  # WILL ECHO
            elif opt == 3:    # SGA
                writer.write(b"\xff\xfb\x03")  # WILL SGA
            else:
                writer.write(bytes([0xff, 0xfe, opt]))  # WONT — unsupported
        # WONT (0xfc) / DONT (0xfe): nothing to reply

    async def _read_data_byte(self, reader, writer=None) -> bytes:
        """Read the next *data* byte, transparently consuming & replying to any
        telnet negotiation in the stream. Returns a length-1 `bytes`, or b"" on EOF.
        All application loops must read through this — never `reader.read(1)` raw."""
        while True:
            b = await reader.read(1)
            if not b:
                return b""
            if b == b"\xff":  # IAC — handle full sequence, keep fetching
                await self._handle_iac(reader, writer)
                continue
            return b

    async def _wait_key(self, reader, writer):
        """Wait for any single data key press and discard it."""
        await self._read_data_byte(reader, writer)

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
            b = await self._read_data_byte(reader, writer)
            if not b:
                return False
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
        # NO proactive IAC negotiation. Vintage clients (e.g. Mocha Telnet on PalmOS)
        # simply render unsolicited IAC sequences as literal text ("HOST: IAC WILL
        # TN_SUPPRESS_GA") and their state machine derails — menu input breaks and the
        # session dies on the client's idle timeout. Let the client drive: it keeps its
        # own local echo (Mocha has 回显 ON), and we take over echo only when the client
        # explicitly requests it via DO ECHO (see _respond_nego → self._server_echo).
        self._server_echo = False

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
        """BBS main menu loop. Reads selection keys via the IAC-safe reader.
        Echoes the selection only if the server owns echo (client asked via DO ECHO)."""
        while True:
            self._write(writer, render_banner())
            await self._flush(writer)

            b = await self._read_data_byte(reader, writer)
            if not b:
                break  # disconnected

            try:
                key = b.decode(self.encoding).strip().lower()
            except UnicodeDecodeError:
                continue

            # Echo the selection only when the server owns echo; otherwise the
            # client does local echo and an extra echo would double it.
            if self._server_echo:
                self._write(writer, key)
                await self._flush(writer)

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

        # Show the input prompt immediately (render_chat_intro does NOT include it) —
        # otherwise a fresh session waits silently until the user hits bare Enter.
        self._write(writer, C_GREEN + "[You]" + R + " > ")
        await self._flush(writer)

        input_str = ""

        # A line-buffered client sends the menu selection as "1\r"; the trailing CR/LF is
        # still buffered here and would fire the empty-Enter branch → a spurious blank
        # prompt right after entry. Peek briefly and swallow one leading CR/LF.
        try:
            lead = await asyncio.wait_for(
                self._read_data_byte(reader, writer), timeout=0.2
            )
        except asyncio.TimeoutError:
            lead = b""
        if lead and lead not in (b"\r", b"\n"):
            # Real first input char (char-mode client): fold it into the input line.
            input_str += lead.decode(self.encoding, errors="ignore")
            if self._server_echo:
                writer.write(lead)
                await self._flush(writer)

        while True:
            try:
                b = await self._read_data_byte(reader, writer)
                if not b:
                    return  # disconnected

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
                    # Multi-byte char (GBK/UTF-8): keep pulling IAC-safe data bytes
                    char_bytes = b
                    while len(char_bytes) < 5:
                        more = await self._read_data_byte(reader, writer)
                        if not more:
                            break
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
                if self._server_echo:
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