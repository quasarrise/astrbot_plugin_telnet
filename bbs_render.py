"""
BBS screen renderer for Telnet Gateway.

Pure functions, no AstrBot dependency — testable standalone.
"""

# ANSI 16色 (兼容 ANSI.SYS / DOS / Win9x)
R = "\x1b[0m"
B = "\x1b[1m"
D = "\x1b[2m"

C_BLACK   = "\x1b[30m"
C_RED     = "\x1b[31m"
C_GREEN   = "\x1b[32m"
C_YELLOW  = "\x1b[33m"
C_BLUE    = "\x1b[34m"
C_MAGENTA = "\x1b[35m"
C_CYAN    = "\x1b[36m"
C_WHITE   = "\x1b[37m"

SCREEN_W = 46
CONTENT_W = SCREEN_W - 2  # between | borders


def _clr() -> str:
    return "\x1b[2J\x1b[H"


def _eol() -> str:
    return "\r\n"


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes for length calculation."""
    out = []
    in_esc = False
    for ch in text:
        if ch == "\x1b":
            in_esc = True
        elif in_esc and ch == "m":
            in_esc = False
        elif not in_esc:
            out.append(ch)
    return "".join(out)


def _center(text: str, width: int = CONTENT_W) -> str:
    if len(text) >= width:
        return text
    left = (width - len(text)) // 2
    return " " * left + text


def _box_border(left: str, mid: str, right: str) -> str:
    return C_CYAN + left + mid * (SCREEN_W - 2) + right + R + _eol()


def _box_title(text: str) -> str:
    # Strip ANSI codes so len() gives the actual visible width
    visible = _strip_ansi(text)
    inner = " " + text + " "
    visible_len = len(visible) + 2  # the two spaces
    while visible_len < CONTENT_W:
        inner = " " + inner + " "
        visible_len += 2
    if visible_len > CONTENT_W:
        # Trim extra space
        inner = inner[:-(visible_len - CONTENT_W)]
    return C_CYAN + "|" + R + inner + C_CYAN + "|" + R + _eol()


def _box_line(inner: str) -> str:
    clean = _strip_ansi(inner)
    pad = CONTENT_W - len(clean)
    if pad < 0:
        pad = 0
    return C_CYAN + "|" + R + inner + " " * pad + C_CYAN + "|" + R + _eol()


def _box_menu(num: int, desc: str) -> str:
    return _box_line(f"{B}{C_YELLOW}[{num}]{R}  {C_WHITE}{desc}{R}")


def _box_hline(char: str = "-") -> str:
    return _box_line(C_CYAN + " " + char * (CONTENT_W - 2) + " ")


def _assemble(parts: list) -> str:
    return "".join(parts)


# ── Pages ────────────────────────────────────

def render_banner() -> str:
    return _assemble([
        _clr(),
        _box_border(".", "-", "."),
        _box_title(f"{B}{C_CYAN}Welcome to AstrBot BBS{R}"),
        _box_title(f"{C_GREEN}Telnet Gateway{R}"),
        _box_border(":", "-", ":"),
        _box_menu(1, "Chat with AI"),
        _box_menu(2, "Announcements"),
        _box_menu(3, "About"),
        _box_menu(4, "Help"),
        _box_menu(5, "Exit"),
        _box_border(":", "-", ":"),
        "",
        C_YELLOW + _center("Select [1-5]: ") + R + _eol(),
        C_WHITE + _center("> ", CONTENT_W - 4) + R,
    ])


def render_chat_intro() -> str:
    return _assemble([
        _clr(),
        _box_border(".", "-", "."),
        _box_title(f"{B}{C_GREEN}Chat Mode{R}"),
        _box_border(":", "-", ":"),
        _box_line(f"{C_YELLOW}/menu{R}  Return to main menu"),
        _box_line(f"{C_YELLOW}/help{R}  Show commands"),
        _box_line(f"{C_YELLOW}/clear{R} Clear screen"),
        _box_border("'", "-", "'"),
    ])


def render_about() -> str:
    return _assemble([
        _clr(),
        _box_border(".", "-", "."),
        _box_title(f"{B}{C_CYAN}About{R}"),
        _box_border(":", "-", ":"),
        _box_line("AstrBot BBS Telnet Gateway"),
        _box_line("Version 1.3.0"),
        _box_line("Author: quasarrise"),
        _box_line("License: MIT"),
        _box_border(":", "-", ":"),
        _box_line(f"{D}Talk to AI from vintage terminals.{R}"),
        _box_line(f"{D}Supports DOS / Win9x / macOS / Linux.{R}"),
        _box_border(":", "-", ":"),
        _box_line(f"{C_CYAN}{B}Press any key to return...{R}"),
        _box_border("'", "-", "'"),
    ])


def render_help() -> str:
    return _assemble([
        _clr(),
        _box_border(".", "-", "."),
        _box_title(f"{B}{C_CYAN}Help{R}"),
        _box_border(":", "-", ":"),
        C_WHITE + _center("Chat Commands") + R + _eol(),
        f"  {C_YELLOW}/menu{R}       Return to main menu" + _eol(),
        f"  {C_YELLOW}/help{R}       Show this help" + _eol(),
        f"  {C_YELLOW}/clear{R}      Clear the screen" + _eol(),
        _box_hline(),
        C_WHITE + _center("Connection Info") + R + _eol(),
        f"  {D}Encoding: UTF-8{R}" + _eol(),
        _box_border(":", "-", ":"),
        _box_line(f"{C_CYAN}{B}Press any key to return...{R}"),
        _box_border("'", "-", "'"),
    ])


def render_announcements() -> str:
    return _assemble([
        _clr(),
        _box_border(".", "-", "."),
        _box_title(f"{B}{C_BLUE}Announcements{R}"),
        _box_border(":", "-", ":"),
        C_WHITE + _center("v1.3.0 — BBS Interface!") + R + _eol(),
        f"  {D}BBS-style menus + colored output{R}" + _eol(),
        f"  {D}/menu to return to main menu{R}" + _eol(),
        _box_hline(),
        C_WHITE + _center("v1.1.0 — Password & IAC") + R + _eol(),
        f"  {D}Added password authentication{R}" + _eol(),
        f"  {D}IAC negotiation for Win11/MobaXterm{R}" + _eol(),
        _box_hline(),
        C_WHITE + _center("v1.0.0 — Initial Release") + R + _eol(),
        f"  {D}Basic telnet gateway for AstrBot{R}" + _eol(),
        _box_border(":", "-", ":"),
        _box_line(f"{C_CYAN}{B}Press any key to return...{R}"),
        _box_border("'", "-", "'"),
    ])
