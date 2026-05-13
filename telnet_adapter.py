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
        "host": "0.0.0.0",
        "port": 2323,
        "encoding": "gbk",
        "password": "",
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
        self.host = self.config.get("host", "0.0.0.0")
        self.port = int(self.config.get("port", 2323))
        self.encoding = self.config.get("encoding", "gbk").lower()
        self.password = self.config.get("password", "").strip()

        logger.info(
            f"[Telnet] 适配器配置加载成功: Host={self.host}, Port={self.port}, Encoding={self.encoding}"
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

    async def handle_client(self, reader, writer):
        # 暂时关闭主动协商，防止超级终端显示乱码
        # writer.write(bytes([255, 251, 1]))
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

                # --- 新增：过滤 Telnet 协议指令 (以 255/0xff 开头) ---
                if char_bytes == b"\xff":
                    # 读取接下来的两个指令字节并丢弃，不进入业务逻辑
                    await reader.read(2)
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
                    # 尝试解码
                    char_text = char_bytes.decode(self.encoding)
                except UnicodeDecodeError:
                    # 多字节编码（如 UTF-8 需要 2~4 字节）
                    # 持续读取字节直到能成功解码或达到安全上限
                    while len(char_bytes) < 5:  # UTF-8 最长 4 字节，留足余量
                        more = await reader.read(1)
                        if not more:
                            break
                        char_bytes += more
                        try:
                            char_text = char_bytes.decode(self.encoding)
                            break
                        except UnicodeDecodeError:
                            continue
                    else:
                        # 超过最大长度仍无法解码，丢弃这些字节
                        continue

                input_str += char_text
                # 回显时增加 errors='ignore'，彻底杜绝 \ufffd 导致的编码崩溃
                writer.write(char_text.encode(self.encoding, errors="ignore"))
                await writer.drain()

            except Exception as e:
                # 使用 logger 而不是 print，避免混淆
                logger.error(f"[Telnet] Loop Error: {e}")
                break
