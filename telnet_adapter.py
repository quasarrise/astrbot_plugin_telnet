import asyncio
from astrbot.api.platform import Platform, AstrBotMessage, MessageMember, PlatformMetadata, MessageType
from astrbot.api.message_components import Plain
from astrbot.api.platform import register_platform_adapter
from .telnet_event import TelnetMessageEvent
from astrbot.api import logger  # 导入日志器


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
            f"[Telnet] 适配器配置加载成功: Host={self.host}, Port={self.port}, "
            f"Encoding={self.encoding}"
        )

    def meta(self) -> PlatformMetadata:
        """
        必须包含 id, name 和 description。
        这些信息会被框架用于 WebUI 展示和后台统计。
        """
        return PlatformMetadata(
            id="telnet",
            name="Telnet Gateway",
            description="允许通过 Telnet 协议连接的旧式终端设备",  # 补全此参数
            logo_path="./logo.png",
            support_streaming_message=False,
        )

    async def process_to_llm(self, msg_str: str, writer):
        """
        辅助方法：将清理后的字符串打包成事件提交给 AstrBot 核心
        """
        try:
            # 获取客户端信息（用于 session_id）
            peer = writer.get_extra_info("peername")
            client_ip = str(peer[0])

            # 1. 构造符合 v4.24.2 要求的 AstrBotMessage
            abm = AstrBotMessage()
            abm.type = MessageType.FRIEND_MESSAGE
            abm.message_str = msg_str
            abm.message = [Plain(text=msg_str)]
            abm.sender = MessageMember(user_id=client_ip, nickname=f"User_{peer[1]}")
            abm.session_id = client_ip
            abm.message_id = str(int(asyncio.get_event_loop().time()))

            # 2. 构造并提交事件
            # 这里的 TelnetMessageEvent 必须接收 writer，以便回复时能找到出口
            event = TelnetMessageEvent(
                message_str=msg_str,
                message_obj=abm,
                platform_meta=self.meta(),
                session_id=abm.session_id,
                client_writer=writer,
                encoding=self.encoding,
            )

            # 3. 正式进入 AI 逻辑池
            self.commit_event(event)

        except Exception as e:
            logger.error(f"[Telnet] Process to LLM Error: {e}")

    async def run(self):
        # 启动 Telnet 服务
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        async with server:
            await server.serve_forever()

    async def _filter_iac(self, reader, writer):
        """Parse and respond to incoming Telnet IAC negotiations.

        Proactive IAC WILL ECHO + WILL SUPPRESS_GO_AHEAD is sent at
        connection start. This method handles any negotiation responses
        the client sends back.

        IAC commands: WILL=251, WONT=252, DO=253, DONT=254
        """
        cmd = await reader.read(1)
        if not cmd:
            return
        opt = await reader.read(1)
        if not opt:
            return

        opt_byte = ord(opt)

        if opt_byte == 1:  # ECHO
            if cmd == b"\xfd":  # DO: client asks us to echo
                writer.write(b"\xff\xfb\x01")  # IAC WILL ECHO
                await writer.drain()
            elif cmd == b"\xfb":  # WILL: client says it will echo locally
                writer.write(b"\xff\xfe\x01")  # IAC DONT ECHO — "don't, I'll do it"
                writer.write(b"\xff\xfb\x01")  # IAC WILL ECHO — "I'll echo"
                await writer.drain()
        elif opt_byte == 3:  # SUPPRESS_GO_AHEAD
            if cmd == b"\xfd":  # DO: client asks us to suppress GA
                writer.write(b"\xff\xfb\x03")  # IAC WILL SUPPRESS_GO_AHEAD
                await writer.drain()
        # All other options: silently ignore

    async def handle_client(self, reader, writer):
        # Proactive IAC WILL ECHO: tell the client "I'll echo for you, turn off
        # local echo." Required for Win11 telnet and Termius which don't initiate
        # ECHO negotiation on their own.
        writer.write(b"\xff\xfb\x01")  # IAC WILL ECHO
        writer.write(b"\xff\xfb\x03")  # IAC WILL SUPPRESS_GO_AHEAD
        await writer.drain()

        writer.write(
            f"Connected. [{self.encoding.upper()} Mode]\r\n".encode(
                self.encoding, errors="ignore"
            )
        )
        await writer.drain()

        # --- Password authentication (if configured) ---
        if self.password:
            writer.write(b"Password: ")
            await writer.drain()
            pwd = ""
            while True:
                b = await reader.read(1)
                if not b:
                    writer.close()
                    await writer.wait_closed()
                    return
                # Filter Telnet IAC sequences (e.g. MobaXterm negotiation)
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
                writer.write(b"\r\nAccess denied.\r\n")
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return
            writer.write(b"\r\nOK.\r\n> ")
            await writer.drain()
        else:
            writer.write(b"> ")
            await writer.drain()

        input_str = ""

        while True:
            try:
                char_bytes = await reader.read(1)
                if not char_bytes:
                    break

                # --- Filter Telnet IAC sequences (adaptive echo negotiation) ---
                if char_bytes == b"\xff":
                    await self._filter_iac(reader, writer)
                    continue

                # 处理退格
                if char_bytes in (b"\x08", b"\x7f"):
                    if input_str:
                        last_char = input_str[-1]
                        input_str = input_str[:-1]

                        # 修正：针对超级终端简化擦除逻辑
                        # 很多旧终端对 \x08 的处理会自动跟随字符宽度
                        # 我们只发送一组退格序列，如果发现删不干净再微调
                        writer.write(b"\x08 \x08")
                        await writer.drain()
                    continue

                # 处理回车
                if char_bytes in (b"\r", b"\n"):
                    if not input_str.strip():
                        writer.write(b"\r\n> ")
                        await writer.drain()
                        continue

                    final_msg = input_str
                    input_str = ""
                    writer.write(b"\r\n[Wait...]\r\n")
                    await writer.drain()
                    await self.process_to_llm(final_msg, writer)
                    continue

                # --- 普通字符处理 ---
                try:
                    # 尝试用配置的编码解码
                    char_text = char_bytes.decode(self.encoding)
                except UnicodeDecodeError:
                    # 多字节编码（如 UTF-8 需要 2~4 字节）
                    # 持续读取字节直到能成功解码或达到安全上限
                    while len(char_bytes) < 5:  # UTF-8 最长 4 字节，留足余量
                        more = await reader.read(1)
                        if not more:
                            break
                        # Filter IAC sequences inside rescue loop too.
                        # Without this, a 0xFF byte arriving between multi-byte
                        # character fragments pollutes the buffer.
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
                        # 超过最大长度仍无法解码，说明编码不匹配（例如服务端 UTF-8 但客户端用 GBK）
                        hex_str = char_bytes.hex(" ")
                        logger.warning(
                            f"[Telnet] Encoding mismatch detected! "
                            f"Server encoding: {self.encoding.upper()}. "
                            f"Undecodable bytes: {hex_str}. "
                            f"If client is Windows telnet, set server encoding to GBK."
                        )
                        # Echo a '?' so the user sees feedback instead of silence
                        writer.write(b"?")
                        await writer.drain()
                        continue

                input_str += char_text
                # 回显时增加 errors='ignore'，彻底杜绝 \ufffd 导致的编码崩溃
                writer.write(char_text.encode(self.encoding, errors="ignore"))
                await writer.drain()

            except Exception as e:
                # 使用 logger 而不是 print，避免混淆
                logger.error(f"[Telnet] Loop Error: {e}")
                break
