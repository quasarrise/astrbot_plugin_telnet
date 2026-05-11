from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.api.message_components import Plain
from astrbot.api import logger

class TelnetMessageEvent(AstrMessageEvent):
    def __init__(self, message_str: str, message_obj: AstrBotMessage, platform_meta: PlatformMetadata, session_id: str, client_writer, encoding: str):
        # 严格按照官方示例的顺序传递：message_str, message_obj, platform_meta, session_id
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client_writer = client_writer
        self.encoding = encoding

    async def send(self, message: MessageChain):
        """
        AstrBot 分段发送时会多次调用此方法。
        必须确保每次调用都完整写入并 drain。
        """
        try:
            for element in message.chain:
                if isinstance(element, Plain):
                    # 针对 Windows Me 的换行符适配
                    text = element.text.replace("\n", "\r\n")
                    
                    # 添加分段标识，方便在超级终端区分
                    output = f"{text}\r\n"
                    
                    # 检查 writer 是否仍然可用
                    if not self.client_writer.transport.is_closing():
                        self.client_writer.write(output.encode(self.encoding, errors='replace'))
                        # 核心：必须等待写入完成，防止下一段消息冲掉当前缓冲区
                        await self.client_writer.drain()
                    else:
                        logger.warn("[Telnet] Writer closed, message segment lost.")
            
            # 发送完一段后，打印提示符
            self.client_writer.write(b"> ")
            await self.client_writer.drain()
            
        except Exception as e:
            logger.error(f"[Telnet] Send segment error: {e}")
        
        # 必须调用基类，否则框架可能认为发送未完成而不继续下一段
        await super().send(message)

    def get_platform_name(self) -> str:
        return "telnet"