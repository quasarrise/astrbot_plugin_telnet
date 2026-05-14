# astrbot-plugin-telnet

AstrBot Telnet Gateway

Telnet是一个源于1969年的远古协议。在1980年代到1990年代，基于Telnet的BBS曾经风行一时，后来Telnet的功能基本被SSH取代，但是Telnet仍然像活化石一样存在于各种设备上。

因此，通过Telnet协议访问Astrbot网关，理论上你就可以在几乎一切能联网的设备上接入LLM，比如DOS、Win9x、Meego、Windows Mobile。

支持UTF-8、GBK、BIG5编码切换，支持设置登录密码。

由于Telnet历史悠久，终端版本众多，无法一一测试，目前在mTCP（DOS）、超级终端（Windows9x）、Mobaxterm测试通过。

※由于Telnet没有加密机制，请避免在公网上暴露Telnet端口。
