import asyncio
import socket
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
    render_announcements,
)


# ═══════════════════════════════════════════════
# Adapter
# ═══════════════════════════════════════════════

@register_platform_adapter(
    "telnet",
    "Telnet适配器",
    default_config_tmpl={
        "enable": False,
        "id": "telnet",
        "监听地址": "0.0.0.0",
        "端口": 2323,
        "编码": "gbk",
        "连接密码（留空则不验证）": "",
        "回声模式": "server",
        "窗口宽度": 0,
        "汉字后加空格": False,
    },
    config_metadata={
        "监听地址": {"type": "string", "description": "监听地址", "hint": "建议保留 0.0.0.0"},
        "端口": {"type": "number", "description": "监听端口"},
        "编码": {
            "type": "string",
            "description": "汉字编码",
            "options": ["gbk", "big5", "utf-8"],
            "hint": "简中老终端用 gbk；繁中老终端用 big5；现代终端用 utf-8",
        },
        "连接密码（留空则不验证）": {"type": "string", "description": "连接密码", "hint": "留空则不验证"},
        "回声模式": {
            "type": "string",
            "description": "回声模式",
            "options": ["server", "client"],
            "hint": "server=服务器回显(主流客户端支持)；client=客户端本地回显(用于不解析IAC的旧终端，请在终端中开启本地回显)",
        },
        "窗口宽度": {
            "type": "number",
            "description": "窗口宽度（列）",
            "hint": "如果汉字在行尾截断产生乱码，请在此指定宽度。0=自动(用IAC上报的客户端宽度,不报退80)。",
        },
        "汉字后加空格": {
            "type": "bool",
            "description": "汉字后加空格",
            "hint": "给按半宽渲染汉字的客户端每个汉字后补空格。",
        },
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
        # 回声模式：server = 主动 IAC WILL ECHO + 无条件回显（服务器回显型客户端：
        #   DOS mTCP/UCDOS、Win11、Termius 等不主动协商、靠服务器回显）
        #   client = 客户端驱动回显，仅当客户端发 DO ECHO 才回显（Mocha PalmOS 等本地回显客户端）
        self.echo_mode = self.config.get("回声模式", self.config.get("echo_mode", "server")).lower()
        # 窗口宽度：>0 = 手动固定列宽；0 = 自动，用 IAC NAWS 协商的客户端真实宽度，
        #   客户端不报则退回 80（DOS VGA 文本模式标准宽度，即 40 个汉字）。
        self.window_width = int(self.config.get("窗口宽度", self.config.get("window_width", 0)))
        # 每个连接的 NAWS 上报宽度（writer → width）
        self._naws_width: dict = {}
        # 汉字后加空格：给按半宽渲染汉字的客户端（Mocha/WM6）在每个 CJK 字符后补空格，
        # 让汉字占满宽度可读；正常终端开这个会浪费半行宽，默认关。
        # WebUI 可能把布尔存成字符串 "true"/"false"，必须正确解析，否则关不掉。
        _pad_raw = self.config.get("汉字后加空格", self.config.get("cjk_pad", False))
        if isinstance(_pad_raw, str):
            self.cjk_pad = _pad_raw.strip().lower() not in ("", "false", "0", "off", "no")
        else:
            self.cjk_pad = bool(_pad_raw)
        # Echo ownership: 默认 server 模式即服务器回显；client 模式下才按需协商。
        self._server_echo = False
        # 监听 server 与常驻信号：run()/terminate() 配合管理优雅重载。
        # 若不处理 terminate，AstrBot 改配置 reload 时旧实例会卡死（详见 run()）。
        self._server = None
        self._stop = None

        logger.info(
            f"[Telnet] BBS mode loaded: Host={self.host}, Port={self.port}, "
            f"Encoding={self.encoding}, EchoMode={self.echo_mode}"
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
            opt = await self._read_chunk(reader)
            if opt is None:
                return
            payload = []
            while True:
                nxt = await self._read_chunk(reader)
                if nxt is None:
                    return
                if nxt == 0xff:  # IAC inside SB: SE (0xf0) ends it, IAC IAC is escaped data
                    nxt2 = await self._read_chunk(reader)
                    if nxt2 is None:
                        return
                    if nxt2 == 0xf0:  # SE — end of sub-negotiation
                        break
                    payload.append(0xff)  # escaped IAC → literal byte
                else:
                    payload.append(nxt)
            # NAWS (RFC1073): opt==0x1f, payload = <w_hi> <w_lo> <h_hi> <h_lo>
            if opt == 0x1f and len(payload) >= 4 and writer is not None:
                width = (payload[0] << 8) | payload[1]
                if width > 0:
                    self._naws_width[writer] = width
                    logger.info(f"[Telnet] NAWS width reported by client: {width}")
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
            elif opt == 0x1f:    # NAWS — client offers to report window size
                writer.write(b"\xff\xfd\x1f")  # DO NAWS (please report it)
            else:
                writer.write(bytes([0xff, 0xfc, opt]))  # WONT — unsupported
        elif cmd == 0xfd:  # client DO <opt>
            if opt == 1:      # ECHO — client asks the server to echo
                self._server_echo = True
                writer.write(b"\xff\xfb\x01")  # WILL ECHO
            elif opt == 3:    # SGA
                writer.write(b"\xff\xfb\x03")  # WILL SGA
            elif opt == 0x1f:    # NAWS — client wants the server to accept window size
                writer.write(b"\xff\xfb\x1f")  # WILL NAWS
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
        """Wait for a meaningful key press and discard it.

        跳过控制字节（mTCP 回车是 CR+NUL，残留的 NUL/LF/CR 若被当成按键
        会让"Press any key"的屏瞬间被吞掉）。直到拿到一个可打印键或断开。
        """
        while True:
            b = await self._read_data_byte(reader, writer)
            if not b:
                return
            if b[0] < 0x20 or b[0] == 0x7f:  # NUL/CR/LF/tab/backspace… 全部跳过
                continue
            return

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

    def _effective_width(self, writer):
        """窗口宽度：手动值 > 0 优先；否则用 NAWS 协商的客户端宽度；再不报则退回 80。"""
        if self.window_width > 0:
            return self.window_width
        nw = self._naws_width.get(writer)
        return nw if nw else 80

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
                wrap_width=self._effective_width(writer),
                cjk_pad=self.cjk_pad,
            )
            self.commit_event(event)
            logger.info(
                f"[Telnet] event committed, session={abm.session_id}, "
                f"msg_len={len(msg_str)} msg={msg_str!r}"
            )
        except Exception as e:
            logger.error(f"[Telnet] Process to LLM Error: {e}")

    # ── main connection handler ─────────────────

    async def handle_client(self, reader, writer):
        # TCP_NODELAY：禁用 Nagle，避免服务器逐字符回显的小包被 Nagle+客户端延迟ACK
        # 拖到几百 ms（局域网 ping 0ms 但回显 0.5~1s 的典型根因）。
        try:
            sock = writer.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        # Echo ownership per echo_mode:
        #  - server (default): proactive IAC WILL ECHO + SGA at connect, server echoes everything.
        #    Clients that don't negotiate or rely on server echo (DOS mTCP/UCDOS, Win11 telnet,
        #    Termius, Windows telnet) get single server-side echo.
        #  - client: no proactive IAC; server echoes only when the client explicitly requests it
        #    via DO ECHO (for local-echo clients like Mocha PalmOS that would double-echo and even
        #    render unsolicited IAC as literal text).
        self._server_echo = False
        if self.echo_mode == "server":
            writer.write(b"\xff\xfb\x01")  # IAC WILL ECHO
            writer.write(b"\xff\xfb\x03")  # IAC WILL SUPPRESS_GO_AHEAD
            writer.write(b"\xff\xfb\x1f")  # IAC WILL NAWS — 请求客户端上报窗口宽度(兼容客户端可用)
            await writer.drain()
            self._server_echo = True

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
                char_text = ""
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

                # 过滤杂散控制字符（mTCP/DOS 回车是 CR+NUL 0d 00，NUL 等不能污染输入行）
                if char_text and all(ord(c) < 32 for c in char_text):
                    continue

                input_str += char_text
                if self._server_echo:
                    writer.write(char_text.encode(self.encoding, errors="ignore"))
                    await self._flush(writer)

            except Exception as e:
                logger.error(f"[Telnet] Chat loop error: {e}")
                return

    # ── server run ──────────────────────────────

    async def terminate(self) -> None:
        """AstrBot 改配置 reload 平台前会 await 此钩子。

        只释放监听端口并通知 run() 让位，不强制断开任何已建立的连接——
        server.close() 同步关监听 socket（立即释放端口，与活连接无关）；
        已有 telnet 会话由各自 handler 协程继续服务，直到用户主动断开。

        此前未实现该钩子：对 run() 用 server.serve_forever() 的旧实现，Python3.12
        里 serve_forever 自身的 finally 会 `await wait_closed()`，被存活的连接拖住，
        使 run task 的取消无法完成、reload 永久挂起 → WebUI 一切换配置就假死，
        只能手动重载插件。run() 改用 Event 常驻后，取消/让位都能干净完成，
        新实例可 bind 同一端口。活连接全程不断开。
        """
        if self._stop is not None:
            self._stop.set()  # 让 run() 的常驻等待优雅让位
        if self._server is not None:
            self._server.close()  # 同步释放监听端口，立即返回，不阻塞 reload
            self._server = None

    async def run(self):
        # 不用 server.serve_forever()：Python3.12 里它自身的 finally 会
        # `await wait_closed()`，被活连接拖住会让 run task 的取消无法完成、
        # AstrBot 重载永久挂起。start_server 返回的 server 已自行启动 accept，
        # 这里只需常驻等待 terminate() 信号，再同步 close 释放端口。
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        self._server = server
        self._stop = asyncio.Event()
        try:
            await self._stop.wait()
        finally:
            server.close()