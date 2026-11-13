from chimericmesh.parsers import format_privmsg, irc_to_event, parse_irc_line, parse_syslog, strip_telnet


def test_syslog_rfc3164():
    ev = parse_syslog("<27>Nov 13 11:10:08 db01 postgres[4121]: FATAL: No space left on device")
    assert (ev.host, ev.app, ev.severity) == ("db01", "postgres", 3)
    assert ev.message.startswith("FATAL")


def test_syslog_rfc5424():
    ev = parse_syslog("<34>1 2026-11-13T11:10:08Z web01 sshd 811 ID47 - Failed password for root")
    assert (ev.host, ev.app, ev.severity) == ("web01", "sshd", 2)
    assert ev.message == "Failed password for root"
    assert ev.ts > 1_700_000_000


def test_syslog_garbage_is_info():
    ev = parse_syslog("not syslog at all", default_host="1.2.3.4")
    assert ev.severity == 6 and ev.host == "1.2.3.4"


def test_irc_privmsg_event():
    ev = irc_to_event(":bob!u@h PRIVMSG #ops :disk error on /dev/sda")
    assert ev and ev.host == "bob" and ev.app == "#ops" and ev.severity == 3


def test_irc_ignores_non_messages():
    assert irc_to_event("PING :server") is None
    assert parse_irc_line("") is None


def test_irc_privmsg_injection_stripped():
    lines = format_privmsg("#ops", "hi\r\nQUIT :pwned")
    assert len(lines) == 1 and "\r" not in lines[0] and "\n" not in lines[0]


def test_telnet_strips_iac_and_ansi():
    data = bytes([255, 251, 1, 255, 250, 24, 1, 255, 240]) + b"\x1b[31mERROR\x1b[0m disk\r\n"
    assert strip_telnet(data).strip() == "ERROR disk"


def test_telnet_escaped_iac_is_data():
    assert strip_telnet(bytes([255, 255])) == "\ufffd"  # 0xFF is invalid utf-8 -> replacement
